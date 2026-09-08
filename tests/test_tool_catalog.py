from bug_agent.tool_catalog import current_fallback_catalog


def test_fallback_catalog_exposes_human_readable_parameter_contracts():
    catalog = current_fallback_catalog({"inspect_case"})
    inspect_case = catalog[0]
    properties = inspect_case["parameters"]["properties"]

    assert "open_case 返回" in properties["case_id"]["description"]
    assert "文件路径" in properties["case_id"]["description"]
    assert "附件样本" in properties["sample_limit"]["description"]


def test_probe_catalog_exposes_auditable_incident_window():
    probe = current_fallback_catalog({"probe_archive_members"})[0]
    properties = probe["parameters"]["properties"]

    assert "调查目标时间窗" in probe["description"]
    assert "人工" in probe["description"]
    assert "事故时间窗" in properties["incident_time_range"]["description"]
    assert "content_time_ranges" in properties["incident_time_range"]["description"]


def test_archive_and_aee_contracts_explain_identifier_handoffs():
    catalog = current_fallback_catalog({"inspect_archive", "extract_aee_db"})
    by_name = {item["name"]: item for item in catalog}

    inspect_properties = by_name["inspect_archive"]["parameters"]["properties"]
    assert "member_offset>0" in inspect_properties["source_sha256"]["description"]
    assert "source_fingerprint.sha256" in inspect_properties["source_sha256"]["description"]

    extract = by_name["extract_aee_db"]
    extract_properties = extract["parameters"]["properties"]
    assert "artifact_id" in extract["description"]
    assert "member_id" in extract["description"]
    assert "members[].artifact_id" in extract_properties["artifact_id"]["description"]
    assert "member_id" in extract_properties["artifact_id"]["description"]


def test_parse_diagnostics_catalog_supports_current_failure_types():
    parse = current_fallback_catalog({"parse_diagnostics"})[0]
    supported = parse["parameters"]["properties"]["diagnostic_types"]["items"]["enum"]

    assert {"watchdog", "kernel_panic", "hung_task", "lmk_oom", "binder_stall"} <= set(supported)
