from bug_agent.tool_catalog import current_fallback_catalog


def test_fallback_catalog_exposes_human_readable_parameter_contracts():
    catalog = current_fallback_catalog({"inspect_case"})
    inspect_case = catalog[0]
    properties = inspect_case["parameters"]["properties"]

    assert "open_case 返回" in properties["case_id"]["description"]
    assert "文件路径" in properties["case_id"]["description"]
    assert "附件样本" in properties["sample_limit"]["description"]
