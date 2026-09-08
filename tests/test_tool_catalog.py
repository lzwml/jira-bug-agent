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
