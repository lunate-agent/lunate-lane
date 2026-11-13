"""Reads of GitHub's state: the milestone's cards, open pull requests, their reviews and checks.

GitHub is the lane's state machine, so these reads are the whole of what the lane knows. Each one is complete
or raises: a list that might be truncated (a next page that was not read, a connection capped at its first
page) is a `GhError`, because a partial list would be read as "nothing else exists".
"""

from __future__ import annotations

import dataclasses
import re
import typing

from scripts import common
from scripts.common import GhError

BRANCH = re.compile(r"^wt/issue-(\d+)$")
PASSED = {"SUCCESS", "NEUTRAL", "SKIPPED"}


class Run(typing.NamedTuple):
    """One check run on a head. `failed_step` is True when a step failed, or the steps could not all be read."""
    name: str
    conclusion: str
    job: int = 0  # the Actions job id, 0 for another app's check
    url: str = ""
    run_id: int = 0  # the workflow run it belongs to, for a rerun
    failed_step: bool = False
    failed_steps: tuple = ()  # names of the steps that failed ("<steps unread>" when they could not all be read)


INFRA = frozenset({"CANCELLED", "TIMED_OUT", "STARTUP_FAILURE"})


@dataclasses.dataclass(frozen=True)
class Card:
    number: int
    title: str
    labels: frozenset
    created: str
    blocked_by: tuple = ()  # open issues this one waits on
    blocking: tuple = ()  # open issues waiting on this one


@dataclasses.dataclass(frozen=True)
class Review:
    author: str
    state: str
    commit: str
    body: str = ""


@dataclasses.dataclass(frozen=True)
class Pull:
    number: int
    author: str
    branch: str
    cards: tuple = ()  # issue numbers it closes
    title: str = ""
    state: str = "OPEN"
    draft: bool = False
    created: str = ""
    head: str = ""
    mergeable: str = ""
    auto_merge: bool = False
    labels: frozenset = frozenset()  # labels of the issues it closes
    reviews: tuple = ()  # Review, oldest first
    checks: tuple = ()  # (name, status, conclusion)
    files: tuple | None = None  # changed paths; None means not read
    in_queue: bool = False  # in the merge queue now
    queue_removal: str = ""  # why the merge queue removed it — only a removal of the current head counts
    disarmed_by: str = ""  # a person who turned auto-merge off last (the latest auto-merge event is theirs)
    node_id: str = ""
    runs: tuple = ()  # Run, for every check run on the head
    review_decision: str = ""  # GitHub's: APPROVED, CHANGES_REQUESTED, REVIEW_REQUIRED, or '' when none is required
    waits_on: tuple = ()  # open issues its card is blocked by: not reviewed, not armed, not re-armed until they close

    @property
    def capabilities(self) -> frozenset:
        """The OpenSpec capabilities this pull request touches (see `capabilities`)."""
        return capabilities(self.files or ())

    @property
    def card(self) -> int | None:
        """The card this pull request carries: its `wt/issue-<n>` branch, else the first issue it closes."""
        match = BRANCH.match(self.branch)
        if match:
            return int(match.group(1))
        return self.cards[0] if self.cards else None


# openspec/specs/<cap>/… and a change's delta, openspec/changes/[archive/]<change>/specs/<cap>/…
SPEC_PATH = re.compile(r"^openspec/(?:specs|changes/(?:archive/)?[^/]+/specs)/([^/]+)/")
FILES_LIMIT = 3000  # GitHub lists at most this many files of one pull request


def capabilities(paths) -> frozenset:
    return frozenset(m.group(1) for m in map(SPEC_PATH.match, paths) if m)


def spec_overlap(p: Pull, others) -> tuple[int, frozenset] | None:
    """The oldest *older*, ready (not draft) open pull request touching one of `p`'s OpenSpec capabilities, and
    the shared ones. A draft does not hold anything back: it may stay unfinished for days (review of #17).

    A pull request whose files were not read cannot be cleared: that is a refusal, not 'no overlap'.
    """
    if p.files is None:
        raise GhError(f"#{p.number}'s files were not read — cannot tell whether it overlaps an older spec change")
    mine = p.capabilities
    if not mine:
        return None
    for other in sorted(others, key=lambda o: o.number):
        if other.number >= p.number:
            break
        if other.draft:
            continue
        if other.files is None:
            raise GhError(f"#{other.number}'s files were not read — cannot clear #{p.number} of a spec overlap")
        shared = mine & other.capabilities
        if shared:
            return other.number, shared
    return None


def _files(cfg: common.Config, number: int, connection: dict) -> tuple:
    """Every changed path of a pull request: the first page from GraphQL, the rest from the REST listing."""
    nodes = connection.get("nodes") or []
    if not (connection.get("pageInfo") or {}).get("hasNextPage"):
        return tuple(str(n["path"]) for n in nodes)
    raw = common.gh("api", "--paginate", f"repos/{cfg.slug}/pulls/{int(number)}/files?per_page=100",
                    "--jq", ".[].filename")
    paths = tuple(line for line in raw.splitlines() if line)
    if len(paths) >= FILES_LIMIT:
        raise GhError(f"#{number} changes {FILES_LIMIT} files or more — GitHub's listing may be cut off")
    if len(paths) <= len(nodes):
        raise GhError(f"#{number}'s file listing came back shorter than its first page")
    return paths


def login(raw: dict | None) -> str:
    """REST says `lunate-worker[bot]`, GraphQL says `lunate-worker`; the lane compares the bare form."""
    name = str((raw or {}).get("login") or "")
    return name[:-5] if name.endswith("[bot]") else name


def _complete(connection: dict, what: str) -> list:
    if (connection.get("pageInfo") or {}).get("hasNextPage"):
        raise GhError(f"{what} has more entries than one page holds — refusing to decide on part of it")
    return connection.get("nodes") or []


def _paged(query: str, path: tuple, what: str, **variables) -> list:
    """Every node of a paginated connection; `path` leads from `data` to the connection."""
    nodes: list = []
    cursor = None
    for _ in range(50):
        data = common.graphql(query, **variables, **({"cursor": cursor} if cursor else {}))
        connection = data
        for key in path:
            connection = (connection or {}).get(key)
        if not isinstance(connection, dict):
            raise GhError(f"{what}: the answer had no {'.'.join(path)}")
        nodes += connection.get("nodes") or []
        page = connection.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return nodes
        cursor = page.get("endCursor")
    raise GhError(f"{what}: more than fifty pages — refusing to decide on part of it")


MILESTONES = """query($owner:String!,$name:String!,$q:String!){repository(owner:$owner,name:$name){
milestones(first:20,query:$q){nodes{title number}}}}"""

ISSUES = """query($owner:String!,$name:String!,$m:String!,$cursor:String){repository(owner:$owner,name:$name){
issues(first:50,after:$cursor,states:OPEN,filterBy:{milestoneNumber:$m},orderBy:{field:CREATED_AT,direction:ASC}){
pageInfo{hasNextPage endCursor} nodes{number title createdAt labels(first:50){pageInfo{hasNextPage} nodes{name}}
blockedBy(first:50){pageInfo{hasNextPage} nodes{number state}} blocking(first:50){pageInfo{hasNextPage} nodes{number state}}}}}}"""


def milestone_cards(cfg: common.Config) -> list[Card]:
    """Every open issue in the configured milestone, with its labels and open dependency edges."""
    data = common.graphql(MILESTONES, owner=cfg.owner, name=cfg.name, q=cfg.milestone)
    found = [m for m in data["repository"]["milestones"]["nodes"] if m.get("title") == cfg.milestone]
    if len(found) != 1:
        raise GhError(f"milestone {cfg.milestone!r} was not found as exactly one open milestone in {cfg.slug}")
    nodes = _paged(ISSUES, ("repository", "issues"), f"milestone {cfg.milestone}",
                   owner=cfg.owner, name=cfg.name, m=str(found[0]["number"]))
    cards = []
    for node in nodes:
        what = f"issue #{node['number']}"
        cards.append(Card(
            number=int(node["number"]), title=str(node.get("title") or ""), created=str(node["createdAt"]),
            labels=frozenset(label["name"] for label in _complete(node["labels"], f"{what}'s labels")),
            blocked_by=tuple(i["number"] for i in _complete(node["blockedBy"], f"{what}'s blockers")
                             if i.get("state") == "OPEN"),
            blocking=tuple(i["number"] for i in _complete(node["blocking"], f"{what}'s dependents")
                           if i.get("state") == "OPEN"),
        ))
    return cards


DECISIONS = """query($owner:String!,$name:String!,$label:String!,$cursor:String){repository(owner:$owner,name:$name){
issues(first:50,after:$cursor,states:OPEN,labels:[$label],orderBy:{field:CREATED_AT,direction:ASC}){
pageInfo{hasNextPage endCursor} nodes{number title createdAt
blocking(first:50){pageInfo{hasNextPage} nodes{number state}}}}}}"""


def decision_issues(cfg: common.Config) -> list[Card]:
    """Every open issue labelled as a decision, in any milestone or none, oldest first, with the open issues it
    blocks."""
    nodes = _paged(DECISIONS, ("repository", "issues"), f"the open {cfg.decision_label} issues",
                   owner=cfg.owner, name=cfg.name, label=cfg.decision_label)
    found = [Card(number=int(n["number"]), title=str(n.get("title") or ""), created=str(n["createdAt"]),
                  labels=frozenset({cfg.decision_label}),
                  blocking=tuple(i["number"] for i in _complete(n["blocking"], f"issue #{n['number']}'s dependents")
                                 if i.get("state") == "OPEN"))
             for n in nodes]
    return sorted(found, key=lambda c: (c.created, c.number))


BLOCKERS = """query($owner:String!,$name:String!,$n:Int!){repository(owner:$owner,name:$name){issue(number:$n){
blockedBy(first:50){pageInfo{hasNextPage} nodes{number title state labels(first:50){pageInfo{hasNextPage} nodes{name}}
comments(last:100){pageInfo{hasPreviousPage} nodes{body authorAssociation}}}}}}}"""

MAINTAINERS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
DECISION = re.compile(r"^\s*\**decision\**\s*:", re.IGNORECASE)


@dataclasses.dataclass(frozen=True)
class Decision:
    number: int
    title: str
    text: str  # the maintainer's latest `Decision:` comment, verbatim; '' when the issue was closed without one


def decisions_for(cfg: common.Config, card: int) -> list[Decision]:
    """For each closed decision issue that blocked `card`, the maintainer's latest comment that starts with
    `Decision:`, verbatim. The card text may not have been updated with it yet."""
    data = common.graphql(BLOCKERS, owner=cfg.owner, name=cfg.name, n=int(card))
    issue = (data.get("repository") or {}).get("issue")
    if not issue:
        raise GhError(f"issue #{card} was not found in {cfg.slug}")
    found = []
    for node in _complete(issue["blockedBy"], f"issue #{card}'s blockers"):
        what = f"issue #{node['number']}"
        labels = {label["name"] for label in _complete(node["labels"], f"{what}'s labels")}
        if node.get("state") != "CLOSED" or cfg.decision_label not in labels:
            continue
        comments = node["comments"]
        said = [c["body"] for c in comments.get("nodes") or []
                if c.get("authorAssociation") in MAINTAINERS and DECISION.match(c.get("body") or "")]
        if not said and (comments.get("pageInfo") or {}).get("hasPreviousPage"):
            raise GhError(f"{what} has more than 100 comments and none of the last 100 is a maintainer's Decision: — "
                          "refusing to brief without it")
        found.append(Decision(int(node["number"]), str(node.get("title") or ""), said[-1].strip() if said else ""))
    return sorted(found, key=lambda d: d.number)


def decisions_section(decisions: list[Decision]) -> str:
    """The briefs' section for `decisions_for`, or '' when the card waited on none."""
    if not decisions:
        return ""
    parts = ["## Decisions this card waited on (verbatim)\n\nA maintainer decided these before the card was started. "
             "The card text may not have been updated with them yet: where the card and a decision disagree, the "
             "decision wins."]
    for d in decisions:
        parts.append(f"### #{d.number} {d.title}\n\n" + (d.text or "(closed with no `Decision:` comment from a "
                                                                    "maintainer — read the issue before relying on it)"))
    return "\n\n".join(parts) + "\n"


def waits_for_approval(p: Pull, required: frozenset, reviewer: str) -> bool:
    """Ready, green, not conflicting, and GitHub still requires an approval the reviewer bot cannot give: the bot
    approved the head (so a maintainer or code owner must), or the pull request carries no card (the bot never
    reviews those)."""
    if (p.state != "OPEN" or p.draft or p.review_decision != "REVIEW_REQUIRED" or p.mergeable == "CONFLICTING"
            or p.waits_on):
        return False
    if check_state(p, required) != "green":
        return False
    return p.card is None or verdict(p, reviewer) == "APPROVED"


OPEN_PULLS = """query($owner:String!,$name:String!,$cursor:String){repository(owner:$owner,name:$name){
pullRequests(first:50,after:$cursor,states:OPEN){pageInfo{hasNextPage endCursor}
nodes{number title isDraft author{login} headRefName closingIssuesReferences(first:10){pageInfo{hasNextPage} nodes{number}}
files(first:100){pageInfo{hasNextPage} nodes{path}}}}}}"""


def open_pulls(cfg: common.Config) -> list[Pull]:
    """Every open pull request, any author, light: number, title, author, branch, the issues it closes, its files."""
    nodes = _paged(OPEN_PULLS, ("repository", "pullRequests"), "the open pull requests",
                   owner=cfg.owner, name=cfg.name)
    return [Pull(number=int(n["number"]), title=str(n.get("title") or ""), author=login(n.get("author")),
                 draft=bool(n.get("isDraft")),
                 branch=str(n.get("headRefName") or ""),
                 cards=tuple(i["number"] for i in _complete(n["closingIssuesReferences"], f"#{n['number']}'s issues")),
                 files=_files(cfg, int(n["number"]), n["files"]))
            for n in nodes]


PULL = """query($owner:String!,$name:String!,$n:Int!){repository(owner:$owner,name:$name){pullRequest(number:$n){
id number title state isDraft createdAt headRefName headRefOid mergeable reviewDecision author{login}
autoMergeRequest{enabledAt}
isInMergeQueue queueRemoval:timelineItems(last:1,itemTypes:[REMOVED_FROM_MERGE_QUEUE_EVENT]){nodes{
... on RemovedFromMergeQueueEvent{createdAt reason beforeCommit{oid}}}}
autoMergeEvents:timelineItems(last:1,itemTypes:[AUTO_MERGE_ENABLED_EVENT,AUTO_MERGE_DISABLED_EVENT]){nodes{__typename
... on AutoMergeDisabledEvent{actor{__typename login}}}}
closingIssuesReferences(first:10){pageInfo{hasNextPage} nodes{number labels(first:50){pageInfo{hasNextPage} nodes{name}}
blockedBy(first:50){pageInfo{hasNextPage} nodes{number state}}}}
reviews(last:100){pageInfo{hasPreviousPage} nodes{author{login} state body commit{oid}}}
files(first:100){pageInfo{hasNextPage} nodes{path}}
commits(last:1){nodes{commit{oid checkSuites(first:50){pageInfo{hasNextPage} nodes{databaseId status createdAt
workflowRun{databaseId runAttempt workflow{databaseId}}}} statusCheckRollup{contexts(first:100){pageInfo{hasNextPage}
nodes{__typename ... on CheckRun{name status conclusion databaseId detailsUrl checkSuite{databaseId status createdAt
workflowRun{databaseId runAttempt workflow{databaseId}}}
steps(first:100){pageInfo{hasNextPage} nodes{name conclusion}}} ... on StatusContext{context state}}}}}}}}}}"""


def pull(cfg: common.Config, number: int) -> Pull:
    """One pull request in full: state, head, auto-merge, reviews and the checks on its head."""
    data = common.graphql(PULL, owner=cfg.owner, name=cfg.name, n=int(number))
    node = (data.get("repository") or {}).get("pullRequest")
    if not node:
        raise GhError(f"pull request #{number} was not found in {cfg.slug}")
    if (node["reviews"].get("pageInfo") or {}).get("hasPreviousPage"):
        raise GhError(f"#{number} has more than 100 reviews — refusing to decide on part of them")
    issues = _complete(node["closingIssuesReferences"], f"#{number}'s issues")
    commits = node["commits"]["nodes"]
    rollup = (commits[0]["commit"].get("statusCheckRollup") if commits else None) or {}
    contexts = _complete(rollup["contexts"], f"#{number}'s checks") if rollup else []
    suites = _complete(commits[0]["commit"]["checkSuites"], f"#{number}'s check suites") if commits else []
    newest = newest_runs([c for c in contexts if c["__typename"] == "CheckRun"], suites)
    checks = tuple((c["name"], c["status"], c.get("conclusion") or "") for c in newest) + tuple(
        (c["context"], "COMPLETED" if c["state"] not in ("PENDING", "EXPECTED") else c["state"], c["state"])
        for c in contexts if c["__typename"] != "CheckRun")
    return Pull(
        number=int(node["number"]), title=str(node.get("title") or ""), state=str(node["state"]),
        draft=bool(node["isDraft"]), created=str(node["createdAt"]), author=login(node.get("author")),
        branch=str(node["headRefName"]), head=str(node["headRefOid"]), mergeable=str(node.get("mergeable") or ""),
        auto_merge=node.get("autoMergeRequest") is not None, cards=tuple(i["number"] for i in issues),
        labels=frozenset(label["name"] for i in issues
                         for label in _complete(i["labels"], f"#{number}'s issue #{i['number']}'s labels")),
        reviews=tuple(Review(login(r.get("author")), str(r["state"]), str((r.get("commit") or {}).get("oid") or ""),
                             str(r.get("body") or "")) for r in node["reviews"]["nodes"]),
        checks=checks, files=_files(cfg, int(number), node["files"]),
        in_queue=bool(node.get("isInMergeQueue")),
        queue_removal=_queue_removal(node), disarmed_by=_disarmed_by(node, cfg.worker_app),
        node_id=str(node.get("id") or ""),
        runs=tuple(_run(c) for c in newest),
        review_decision=str(node.get("reviewDecision") or ""),
        waits_on=_waits_on(node, issues, number),
    )


def _waits_on(node: dict, issues: list, number: int) -> tuple:
    """The open blockers of the card this pull request carries (its `wt/issue-<n>` branch, else the issues it
    closes), from the issues it closes."""
    match = BRANCH.match(str(node.get("headRefName") or ""))
    mine = [i for i in issues if match and i["number"] == int(match.group(1))] or issues
    return tuple(sorted({b["number"] for i in mine
                         for b in _complete(i.get("blockedBy") or {}, f"#{number}'s issue #{i['number']}'s blockers")
                         if b.get("state") == "OPEN"}))


OPEN_BLOCKERS = """query($owner:String!,$name:String!,$n:Int!){repository(owner:$owner,name:$name){issue(number:$n){
blockedBy(first:50){pageInfo{hasNextPage} nodes{number state}}}}}"""


def open_blockers(cfg: common.Config, card: int) -> tuple:
    """The open issues `card` is blocked by (GitHub's native blocked-by). Unreadable raises."""
    data = common.graphql(OPEN_BLOCKERS, owner=cfg.owner, name=cfg.name, n=int(card))
    issue = (data.get("repository") or {}).get("issue")
    if not issue:
        raise GhError(f"issue #{card} was not found in {cfg.slug}")
    return tuple(sorted(b["number"] for b in _complete(issue["blockedBy"], f"issue #{card}'s blockers")
                        if b.get("state") == "OPEN"))


def waits_line(blockers) -> str:
    return "waits on " + ", ".join(f"#{n}" for n in blockers)


def required_checks(cfg: common.Config) -> frozenset:
    """The status-check contexts the base branch's rules require. None found is a refusal, not 'all green'."""
    rules = common.gh_json("api", f"repos/{cfg.slug}/rules/branches/{cfg.base}")
    if not isinstance(rules, list):
        raise GhError(f"the rules for {cfg.base} came back as {type(rules).__name__}, not a list")
    names = frozenset(check["context"] for rule in rules if rule.get("type") == "required_status_checks"
                      for check in (rule.get("parameters") or {}).get("required_status_checks") or [])
    if not names:
        raise GhError(f"no required status checks found on {cfg.base} — refusing to call any head green")
    return names


def check_state(p: Pull, required: frozenset) -> str:
    """green, pending or failing — over the required checks only. A required check not reported is pending."""
    verdict = "green"
    for name in sorted(required):
        runs = [c for c in p.checks if c[0] == name]
        if not runs or any(status != "COMPLETED" for _, status, _ in runs):
            verdict = "pending" if verdict == "green" else verdict
        elif any(conclusion not in PASSED for _, _, conclusion in runs):
            return "failing"
    return verdict


def reviewer_verdict(p: Pull, reviewer: str) -> str | None:
    """The reviewer identity's latest review state on the current head, or None if it has not reviewed it.

    A dismissed review is a withdrawn verdict, so it does not count as the head having been reviewed.
    """
    states = [r.state for r in p.reviews if r.author == reviewer and r.commit == p.head
              and r.state not in ("DISMISSED", "PENDING")]
    return states[-1] if states else None


def verdict(p: Pull, reviewer: str) -> str | None:
    """The verdict the lane acts on at the current head: a person outranks the reviewer bot.

    A dismissed bot review counts for nothing (`reviewer_verdict`). A person's APPROVED on the head after the bot's
    last CHANGES_REQUESTED there (or with no bot verdict at all) is APPROVED: no rework, and a ready, green, unarmed
    pull request is re-armed. A person's own request for changes is theirs to follow up, not a rework trigger, so
    only approvals are read from people.
    """
    on_head = [r for r in p.reviews if r.commit == p.head and r.state in ("APPROVED", "CHANGES_REQUESTED")]
    last_bot_change = max((i for i, r in enumerate(on_head) if r.author == reviewer and r.state == "CHANGES_REQUESTED"),
                          default=-1)
    if any(r.author != reviewer and r.state == "APPROVED" for r in on_head[last_bot_change + 1:]):
        return "APPROVED"
    return reviewer_verdict(p, reviewer)


def person_approved(p: Pull, reviewer: str) -> str:
    """The login of a person (not the reviewer bot) whose approval on the current head stands, or ''."""
    if verdict(p, reviewer) != "APPROVED":
        return ""
    people = [r.author for r in p.reviews if r.commit == p.head and r.state == "APPROVED" and r.author != reviewer]
    return people[-1] if people else ""


def landed_parents(cfg: common.Config, number: int) -> tuple[str, int]:
    """(merge commit, its parent count) of a merged pull request: 1 is a squash, 2 a merge commit (#6)."""
    data = common.graphql("""query($owner:String!,$name:String!,$n:Int!){repository(owner:$owner,name:$name){
pullRequest(number:$n){state mergeCommit{oid parents(first:3){totalCount}}}}}""", owner=cfg.owner, name=cfg.name,
                          n=int(number))
    node = (data.get("repository") or {}).get("pullRequest") or {}
    commit = node.get("mergeCommit")
    if node.get("state") != "MERGED" or not isinstance(commit, dict):
        raise GhError(f"#{number} is {str(node.get('state') or 'unreadable').lower()}, not merged with a commit")
    return str(commit["oid"]), int(commit["parents"]["totalCount"])


REPORT = """query($owner:String!,$name:String!,$n:Int!){repository(owner:$owner,name:$name){pullRequest(number:$n){
comments(last:50){nodes{author{login} body}}}}}"""


def worker_report(cfg: common.Config, number: int) -> str:
    """The worker identity's latest session report on the pull request, or '' when it has posted none."""
    data = common.graphql(REPORT, owner=cfg.owner, name=cfg.name, n=int(number))
    node = (data.get("repository") or {}).get("pullRequest")
    if not node:
        raise GhError(f"pull request #{number} was not found in {cfg.slug}")
    found = [c["body"] for c in node["comments"]["nodes"]
             if login(c.get("author")) == cfg.worker_app and str(c.get("body") or "").startswith("**Worker session**")]
    return found[-1] if found else ""


DONE = "COMPLETED"


def _suite_key(suite: dict) -> tuple:
    run = suite.get("workflowRun") or {}
    return (str(suite.get("createdAt") or ""), int(run.get("runAttempt") or 0), int(suite.get("databaseId") or 0))


def newest_runs(check_runs: list, suites: list) -> list:
    """For each check name, its newest check run on the head — the latest created (check-run ids only grow, so a
    rerun attempt's run and a newer workflow run's run both win) — as GitHub judges a head.

    A check is still pending while a run or attempt of the same workflow that will produce it again is queued or
    in progress: a newer check suite of that workflow, or the same suite re-queued for a rerun, that is not
    completed. That is the state right after a pull request is marked ready: the draft run's deliberate `ci-ok`
    failure is the newest `ci-ok` until the ready run creates its own. The returned runs are copies with
    status IN_PROGRESS and no conclusion where that applies.
    """
    every_suite = {int(s.get("databaseId") or 0): s for s in suites}
    for c in check_runs:
        suite = c.get("checkSuite") or {}
        every_suite.setdefault(int(suite.get("databaseId") or 0), suite)

    def workflow(suite: dict):
        return (((suite.get("workflowRun") or {}).get("workflow")) or {}).get("databaseId")

    latest: dict = {}
    for c in check_runs:
        current = latest.get(c["name"])
        if current is None or int(c.get("databaseId") or 0) > int(current.get("databaseId") or 0):
            latest[c["name"]] = c
    found = []
    for name in sorted(latest):
        c = latest[name]
        own = c.get("checkSuite") or {}
        flow = workflow(own)
        coming = flow is not None and any(
            workflow(s) == flow and s.get("status") not in (None, DONE) and _suite_key(s) >= _suite_key(own)
            for s in every_suite.values())
        if c.get("status") == DONE and coming:
            c = {**c, "status": "IN_PROGRESS", "conclusion": None}
        found.append(c)
    return found


def _run(c: dict) -> Run:
    steps = c.get("steps") or {}
    unread = (steps.get("pageInfo") or {}).get("hasNextPage") or "nodes" not in steps
    names = tuple(str((s or {}).get("name") or "") for s in steps.get("nodes") or []
                  if (s or {}).get("conclusion") == "FAILURE")
    if unread:
        names += ("<steps unread>",)
    run_id = ((c.get("checkSuite") or {}).get("workflowRun") or {}).get("databaseId")
    return Run(str(c["name"]), str(c.get("conclusion") or ""), int(c.get("databaseId") or 0),
               str(c.get("detailsUrl") or ""), int(run_id or 0), bool(names), names)


def _queue_removal(node: dict) -> str:
    """The queue's reason for its latest removal — only when it removed the current head; one before the last
    push is history, not a reason to rework (review of #17)."""
    for event in ((node.get("queueRemoval") or {}).get("nodes") or [])[-1:]:
        if ((event.get("beforeCommit") or {}).get("oid") or "") == node.get("headRefOid"):
            return str(event.get("reason") or "removed")
    return ""


def _disarmed_by(node: dict, worker_app: str) -> str:
    """The login of a person (a User, not an App) whose turning auto-merge off is the latest auto-merge event."""
    for event in ((node.get("autoMergeEvents") or {}).get("nodes") or [])[-1:]:
        actor = event.get("actor") or {}
        if event.get("__typename") == "AutoMergeDisabledEvent" and actor.get("__typename") == "User" \
                and login(actor) != worker_app:
            return login(actor)
    return ""


def failure_kind(p: Pull, required: frozenset, aggregate: tuple | None = None) -> str | None:
    """'infra' or 'real' for a head whose required checks are failing, else None.

    A run is infra when it was cancelled, timed out or failed to start with no failing step. The required
    aggregate check (`ci-ok`, `aggregate` = (check name, its aggregating step), from lane.json) is judged by the
    jobs it rolls up — the other check runs of its workflow run: its failure is infra when the only step that
    failed is the aggregating one and every job beside it that did not succeed is infra; it is real when any
    aggregated job has a failing step, when ci-ok fails at another step, or when it fails with nothing beneath
    it failing. Any other required check that failed for real is real, whatever its legs did. A failure the
    lane cannot attribute (a status context, unread steps, no workflow run) is real: a rerun is only for what
    is plainly the runner's fault.
    """
    if check_state(p, required) != "failing":
        return None
    if aggregate is None:
        cfg = common.config()
        aggregate = (cfg.aggregate_check, cfg.aggregate_step)
    name, step = aggregate
    failing = [r for r in p.runs if r.conclusion and r.conclusion not in PASSED]
    if any(c[0] in required and c[0] not in {r.name for r in p.runs} for c in p.checks if c[2] not in PASSED):
        return "real"  # a required status context, not a check run: nothing to rerun

    def infra(r: Run) -> bool:
        return r.conclusion in INFRA and not r.failed_step and r.run_id > 0

    if not failing:
        return "real"
    for r in failing:
        if r.name == name and r.name in required and not infra(r):
            if r.run_id == 0 or set(r.failed_steps) != {step}:
                return "real"  # ci-ok failed somewhere other than its aggregating step
            jobs = [j for j in p.runs if j.run_id == r.run_id and j.name != name and j.conclusion not in PASSED]
            if not jobs or not all(infra(j) for j in jobs):
                return "real"  # an aggregated job failed for real, or ci-ok failed with nothing beneath it
        elif not infra(r):
            return "real"
    return "infra"


def infra_runs(p: Pull) -> tuple:
    """The workflow runs to rerun for an infra failure: those of the failing check runs, each once."""
    return tuple(sorted({r.run_id for r in p.runs if r.conclusion and r.conclusion not in PASSED and r.run_id}))


def needs_rearm(p: Pull, required: frozenset, reviewer: str) -> bool:
    """A clean, ready, approved pull request that is not armed and not queued — the state a clean finish leaves
    when arming fails after `mark_ready` (review of #17). Not when a person turned auto-merge off, and not when it
    is stuck (that is a rework) or its checks are not green."""
    return (p.state == "OPEN" and not p.draft and not p.auto_merge and not p.in_queue and not p.disarmed_by
            and not p.waits_on
            and p.mergeable != "CONFLICTING" and not p.queue_removal and check_state(p, required) == "green"
            and verdict(p, reviewer) == "APPROVED")


def stuck_reason(p: Pull, required: frozenset, reviewer: str, reran: frozenset = frozenset()) -> str | None:
    """Why an open, ready pull request is stuck and goes back to a worker (#8), or None.

    Red on a required check, CONFLICTING with the base, or approved at its head but neither in the merge queue nor
    armed, with a removal by the queue on record (approved and unarmed without one is a person's choice). A draft is unfinished work, not stuck; UNKNOWN mergeability is not a conflict.
    """
    if p.state != "OPEN" or p.draft:
        return None
    if p.mergeable == "CONFLICTING":
        return "conflicts with the base branch"
    if check_state(p, required) == "failing":
        if failure_kind(p, required) == "real":
            return "required checks failing"
        if (p.number, p.head) in reran:  # `reran`: (pull request, head) pairs already rerun once
            return "required checks cancelled or timed out again after a rerun"
        return None  # the feeder reruns it once instead
    if p.disarmed_by:
        return None  # a person turned auto-merge off: their decision, not a rework (the digest says who)
    if verdict(p, reviewer) == "APPROVED" and not p.in_queue and not p.auto_merge and p.queue_removal:
        return f"out of the merge queue after approval ({p.queue_removal.lower()})"
    return None


def orphaned_draft(p: Pull, running, parked) -> bool:
    """A worker pull request left a draft with nobody on it: no worker running its card, and the card not parked.
    A run that does not finish cleanly leaves its pull request a draft; nothing else moves a draft, so it would
    sit unseen (#816). The feeder relaunches it, with idle runs counted, and parks it at the cap."""
    return p.state == "OPEN" and p.draft and p.card is not None and p.card not in running and p.card not in parked


# only at the start of a line (after markup, a quote or a list marker): a mention mid-sentence — #818's review said
# "this is a worker-fixable omission, not a `NEEDS-PERSON` item." — is not a request for a person
NEEDS_PERSON = re.compile(r"^[ \t>*_`-]*NEEDS-PERSON\b[*_`]*[ \t]*[:—–-]?[ \t]*(.*)", re.MULTILINE)
REASON_LIMIT = 300


def needs_person(text: str) -> str | None:
    """The reason after the first line that starts with `NEEDS-PERSON:` in a worker's report or a review body, or
    None when there is no such line. A finding nobody can meet inside the card — a maintainer's decision, a failure in code the diff
    does not touch, evidence only a real release gives — stops the card for a person instead of another round."""
    match = NEEDS_PERSON.search(text or "")
    if not match:
        return None
    reason = match.group(1).strip().strip("*_` ").strip()
    return reason[:REASON_LIMIT] or "(no reason given)"


class CiRun(typing.NamedTuple):
    """One run of the pull-request CI workflow on a head. `ready_job` is the status or conclusion of the job that
    only runs on a ready pull request ('SKIPPED' on a draft), '' while it does not exist yet."""
    id: int
    status: str
    conclusion: str
    created: str
    ready_job: str

    @property
    def cancelled(self) -> bool:
        return self.conclusion == "CANCELLED"


CI_RUNS = """query($owner:String!,$name:String!,$oid:GitObjectID!,$job:String!){repository(owner:$owner,name:$name){
object(oid:$oid){... on Commit{checkSuites(first:100){pageInfo{hasNextPage} nodes{status conclusion createdAt
workflowRun{databaseId event workflow{name}} checkRuns(first:10,filterBy:{checkName:$job}){nodes{status conclusion}}}}}}}}"""


def ci_runs(cfg: common.Config, head: str) -> list[CiRun]:
    """Every pull-request run of the CI workflow (`checks.workflow`) on `head`, oldest first. An unreadable head is
    a GhError, never 'no runs'."""
    data = common.graphql(CI_RUNS, owner=cfg.owner, name=cfg.name, oid=head, job=cfg.ready_job)
    commit = (data.get("repository") or {}).get("object")
    if not isinstance(commit, dict) or "checkSuites" not in commit:
        raise GhError(f"the commit {head[:9]} was not found in {cfg.slug}")
    found = []
    for suite in _complete(commit["checkSuites"], f"{head[:9]}'s check suites"):
        run = suite.get("workflowRun") or {}
        if (run.get("workflow") or {}).get("name") != cfg.ci_workflow or run.get("event") != "pull_request":
            continue
        jobs = (suite.get("checkRuns") or {}).get("nodes") or []
        job = jobs[-1] if jobs else {}
        found.append(CiRun(int(run.get("databaseId") or 0), str(suite.get("status") or ""),
                           str(suite.get("conclusion") or ""), str(suite.get("createdAt") or ""),
                           str(job.get("conclusion") or job.get("status") or "")))
    return sorted(found, key=lambda r: r.id)


def ready_run(r: CiRun, draft_runs: frozenset) -> bool:
    """Whether a CI run saw the pull request as ready: its ready-only job ran (anything but SKIPPED). Before that
    job exists — a run cancelled while queued never gets it — a run that was not on the head yet when the pull
    request was marked ready (`draft_runs`) is the ready_for_review run."""
    if r.ready_job:
        return r.ready_job != "SKIPPED"
    return r.id not in draft_runs


RELEASE_BRANCH = re.compile(r"^release/[^/]+$")
RELEASE_TITLE = "chore(release)"


def is_release(branch: str, title: str) -> bool:
    """A release pull request — branch `release/<version>`, or a title starting with `chore(release)` — may edit
    CHANGELOG.md (it collects the fragments) and needs no `changes/<n>.md` of its own. Who opens and merges
    release pull requests is not decided yet; this is only the exception in both briefs."""
    return bool(RELEASE_BRANCH.match(branch or "")) or (title or "").strip().startswith(RELEASE_TITLE)
