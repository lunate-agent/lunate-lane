"""Reads of GitHub's state: the milestone's cards, open pull requests, their reviews and checks.

GitHub is the lane's state machine, so these reads are the whole of what the lane knows. Each one is complete
or raises: a list that might be truncated (a next page that was not read, a connection capped at its first
page) is a `GhError`, because a partial list would be read as "nothing else exists".
"""

from __future__ import annotations

import dataclasses
import re

from scripts import common
from scripts.common import GhError

BRANCH = re.compile(r"^wt/issue-(\d+)$")
PASSED = {"SUCCESS", "NEUTRAL", "SKIPPED"}


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

    @property
    def card(self) -> int | None:
        """The card this pull request carries: its `wt/issue-<n>` branch, else the first issue it closes."""
        match = BRANCH.match(self.branch)
        if match:
            return int(match.group(1))
        return self.cards[0] if self.cards else None


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


OPEN_PULLS = """query($owner:String!,$name:String!,$cursor:String){repository(owner:$owner,name:$name){
pullRequests(first:50,after:$cursor,states:OPEN){pageInfo{hasNextPage endCursor}
nodes{number author{login} headRefName closingIssuesReferences(first:10){pageInfo{hasNextPage} nodes{number}}}}}}"""


def open_pulls(cfg: common.Config) -> list[Pull]:
    """Every open pull request, any author, light: number, author, branch and the issues it closes."""
    nodes = _paged(OPEN_PULLS, ("repository", "pullRequests"), "the open pull requests",
                   owner=cfg.owner, name=cfg.name)
    return [Pull(number=int(n["number"]), author=login(n.get("author")), branch=str(n.get("headRefName") or ""),
                 cards=tuple(i["number"] for i in _complete(n["closingIssuesReferences"], f"#{n['number']}'s issues")))
            for n in nodes]


PULL = """query($owner:String!,$name:String!,$n:Int!){repository(owner:$owner,name:$name){pullRequest(number:$n){
number title state isDraft createdAt headRefName headRefOid mergeable author{login} autoMergeRequest{enabledAt}
closingIssuesReferences(first:10){pageInfo{hasNextPage} nodes{number labels(first:50){nodes{name}}}}
reviews(last:100){pageInfo{hasPreviousPage} nodes{author{login} state body commit{oid}}}
commits(last:1){nodes{commit{oid statusCheckRollup{contexts(first:100){pageInfo{hasNextPage}
nodes{__typename ... on CheckRun{name status conclusion} ... on StatusContext{context state}}}}}}}}}}"""


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
    checks = tuple((c["name"], c["status"], c.get("conclusion") or "") if c["__typename"] == "CheckRun"
                   else (c["context"], "COMPLETED" if c["state"] not in ("PENDING", "EXPECTED") else c["state"],
                         c["state"]) for c in contexts)
    return Pull(
        number=int(node["number"]), title=str(node.get("title") or ""), state=str(node["state"]),
        draft=bool(node["isDraft"]), created=str(node["createdAt"]), author=login(node.get("author")),
        branch=str(node["headRefName"]), head=str(node["headRefOid"]), mergeable=str(node.get("mergeable") or ""),
        auto_merge=node.get("autoMergeRequest") is not None, cards=tuple(i["number"] for i in issues),
        labels=frozenset(label["name"] for i in issues for label in i["labels"]["nodes"]),
        reviews=tuple(Review(login(r.get("author")), str(r["state"]), str((r.get("commit") or {}).get("oid") or ""),
                             str(r.get("body") or "")) for r in node["reviews"]["nodes"]),
        checks=checks,
    )


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
