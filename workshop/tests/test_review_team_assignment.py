import pytest

from agent_workshop import reviews_skills as reviews
from agent_workshop.admin import enroll
from agent_workshop.auth import Denied, authenticate, connect
from agent_workshop.organization import TOOLS
from agent_workshop.review_admin import subject
from test_reviews_skills import team


def test_automatic_and_manual_assignment_follow_the_review_leads_team(team):
    dsn, identity = team
    manager = identity("manager")
    with connect(dsn) as conn:
        hire = TOOLS["aw_hire"][2]
        hire(conn, manager, expected_revision=1, operation_id="hire-lead",
             employee_id="second-lead", role_id="review_lead", supervisor_id="manager", team_id="audit-2")
        hire(conn, manager, expected_revision=2, operation_id="hire-specialist",
             employee_id="second-reviewer", role_id="reviewer", supervisor_id="second-lead", team_id="audit-2")
        subject(conn, manager.org_id, "second-reviewer", "independent-second")
        token = enroll(conn, manager.installation_id, manager.org_id, "second-lead", 1, 1)
        lead = authenticate(conn, token)
        review = reviews.review_request(conn, lead, "auto", "text", "v1", "PROSE", "SAMPLED", 60)
        assert reviews.get_review(conn, lead, review["review_id"])["reviewer_id"] == "second-reviewer"
        with pytest.raises(Denied, match="INDEPENDENT_REVIEW_REQUIRED"):
            reviews.review_request(conn, lead, "manual", "text", "v1", "DIRECTION", "SAMPLED",
                                   60, reviewer_id="reviewer")
        direct = reviews.review_request(conn, identity("review-lead"), "direct", "text", "v1",
                                        "DIRECTION", "SAMPLED", 60, reviewer_id="reviewer")
        assert reviews.get_review(conn, identity("review-lead"), direct["review_id"])["reviewer_id"] == "reviewer"

        # Suspending the original reviewer preserves which team owned that tenure.
        TOOLS["aw_suspend"][2](conn, manager, expected_revision=3, operation_id="suspend",
                               employee_id="second-reviewer")
        hire(conn, manager, expected_revision=4, operation_id="hire-relief",
             employee_id="relief", role_id="reviewer", supervisor_id="second-lead", team_id="audit-2")
        subject(conn, manager.org_id, "relief", "independent-relief")
        with pytest.raises(Denied, match="REVIEW_DENIED"):
            reviews.reconcile_review(conn, identity("review-lead"), "unrelated-team",
                                     review["review_id"], "reviewer")
        with pytest.raises(Denied, match="INDEPENDENT_REVIEW_REQUIRED"):
            reviews.reconcile_review(conn, lead, "wrong-relief", review["review_id"], "reviewer")
        assert reviews.reconcile_review(conn, lead, "replace", review["review_id"], "relief")["reviewer_id"] == "relief"
