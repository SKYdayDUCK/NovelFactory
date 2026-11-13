"""Deterministic review disposition shared by production and offline flow checks."""


def blocking_review_issues(issues):
    return [issue for issue in issues if issue["disposition"] != "RESOLVED"
            and (issue["category"] == "HARD_CONSISTENCY" or issue["issue_kind"] == "HARD_CONFLICT"
                 or issue["severity"] == "BLOCKER" or
                 (issue["issue_kind"] == "CHANGE_REQUEST" and issue["disposition"] in {"OPEN", "ACCEPT"}))]
