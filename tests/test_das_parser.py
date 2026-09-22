from smartgpms.das_parser import find_gate_postback, parse_cpm_detail, parse_gate_detail


def test_parse_cpm_detail_and_photos():
    source = """
    <span id="lbCpmId">9012</span><span id="lbCntrNo">MSCU6639870</span>
    <span id="lbSealNo">SEAL8899</span><span id="lbStatus">4</span>
    <tr id="TR_STEP_U1"><td><a href="/photo/u1.jpg">F1</a></td></tr>
    <tr id="TR_STEP_S1"><td><a href="/photo/s1.jpg">F2</a></td></tr>
    """
    result = parse_cpm_detail(source, "http://das.example/detail")
    assert result.cpm_id == "9012"
    assert result.container_no == "MSCU6639870"
    assert result.seal_no == "SEAL8899"
    assert result.business_stage == 4
    assert result.photos[-1].source_url == "http://das.example/photo/s1.jpg"


def test_status_stage_and_thumbnail_are_handled():
    source = """
    <span id="lbCpmId">9013</span><span id="lbCntrNo">MSCU6639870</span>
    <span id="lbStatus">Stage 4 complete</span>
    <tr id="TR_STEP_U1"><td><a href="/photo/original.jpg"><img src="/thumb/small.jpg"></a></td></tr>
    """
    result = parse_cpm_detail(source, "http://das.example/detail")
    assert result.business_stage == 4
    assert [photo.source_url for photo in result.photos] == [
        "http://das.example/photo/original.jpg"
    ]


def test_gate_detail_and_dynamic_postback():
    source = """
    <table><tr><td>MSCU6639870</td><td><a href="javascript:__doPostBack('dgMain$ctl17$ctl00','')">查看详细</a></td></tr></table>
    <span id="lbl_container_no">MSCU6639870</span><span id="lbl_seal_no">SEAL8899</span>
    """
    assert find_gate_postback(source, "MSCU6639870") == "dgMain$ctl17$ctl00"
    assert parse_gate_detail(source) == {
        "container_no": "MSCU6639870",
        "seal_no": "SEAL8899",
    }


def test_postback_requires_exact_hidden_container():
    source = """
    <tr><td>MSCU6639870 mentioned</td><td><input id="x_hddCntrNo_0" value="OTHER000001">
    <a href="javascript:__doPostBack('wrong','')">查看详细</a></td></tr>
    <tr><td><input id="x_hddCntrNo_1" value="MSCU6639870">
    <a href="javascript:__doPostBack('correct','')">查看详细</a></td></tr>
    """
    assert find_gate_postback(source, "MSCU6639870") == "correct"
