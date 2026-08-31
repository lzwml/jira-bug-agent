from bug_agent.rca_state import Claim, RCAState, stable_claim_id


def test_claim_id_is_stable_and_state_round_trips():
    claim_id = stable_claim_id("root_cause", "  SurfaceFlinger   fatal ")
    claim = Claim(claim_id=claim_id, type="root_cause", statement="SurfaceFlinger fatal", created_run_id="run-1")
    state = RCAState(revision=1, updated_at="now", based_on_runs=["run-1"], conclusion_status="hypothesis_only", summary="x", claims=[claim])
    restored = RCAState.model_validate_json(state.model_dump_json())
    assert restored.claims[0].claim_id == claim_id
    assert stable_claim_id("root_cause", "SurfaceFlinger fatal") == claim_id
