from log_analysis_core import (
    classify_diagnostic_line,
    extract_timestamp,
    stable_id,
)


def test_extracts_android_and_kernel_clock_domains():
    android = extract_timestamp("08-26 10:20:31.123 I SurfaceFlinger: ready", 2026)
    kernel = extract_timestamp("[  123.456789] Call Trace:")
    assert android.clock_domain == "android"
    assert android.normalized == "2026-08-26T10:20:31.123000"
    assert kernel.clock_domain == "kernel_monotonic"
    assert kernel.relative_seconds == 123.456789


def test_avc_parser_extracts_structured_attributes():
    item = classify_diagnostic_line(
        "avc: denied { read write } scontext=u:r:a:s0 tcontext=u:object_r:b:s0 tclass=file",
        {"avc"},
    )
    assert item.diagnostic_type == "avc"
    assert item.attributes["permissions"] == ["read", "write"]
    assert item.attributes["tclass"] == "file"


def test_classification_is_filtered_by_requested_types():
    assert classify_diagnostic_line("FATAL EXCEPTION: main", {"anr"}) is None
    assert classify_diagnostic_line("FATAL EXCEPTION: main", {"fatal"}).severity == "critical"


def test_stable_id_is_repeatable_and_namespaced():
    assert stable_id("evidence", "same") == stable_id("evidence", "same")
    assert stable_id("evidence", "same").startswith("evidence_")

