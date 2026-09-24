from smartgpms.das_parser import (
    find_cpm_id,
    find_gate_postback,
    find_latest_cpm_id,
    parse_cpm_detail,
    parse_cpm_search_rows,
    parse_gate_detail,
    parse_gate_search_rows,
    parse_process_status,
)


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
    assert result.photos[-1].label == "F2"


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


def test_das_process_status_supports_seal_confirmation():
    assert parse_process_status("5.铅封确认") == 5
    assert parse_process_status("铅封确认") == 5
    assert parse_process_status("4.封箱检查") == 4


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


def test_parse_gate_search_rows_extracts_print_cache_metadata():
    source = """
    <table id="dgMain"><tr><th>序号</th></tr><tr>
      <td>1</td><td><a href="javascript:__doPostBack('dgMain$ctl03$ctl00','')">查看详细</a></td>
      <td>2026-09-24</td><td>OWM</td><td>ODL26091200225</td><td>厂家</td>
      <td></td><td></td><td>备注</td><td>CAAU9080182</td><td>CN8463783</td>
      <td>155</td><td><a href="R_EGT_GERPTMRESERVEDETAIL.aspx?Regdate=20260924&amp;Seqno=2955421&amp;MailSeq=ODL26091200225">返出批准</a></td>
      <td>2026-09-24 09:51</td><td></td>
    </tr></table>
    """
    rows = parse_gate_search_rows(source, "http://das/eGate/tm/search.aspx")

    assert rows == [
        {
            "application_date": "2026-09-24",
            "gate_type": "OWM",
            "gate_pass_no": "ODL26091200225",
            "vendor_name": "厂家",
            "vehicle_no": "",
            "returner": "",
            "remark": "备注",
            "container_no": "CAAU9080182",
            "seal_no": "CN8463783",
            "return_quantity": "155",
            "process_status": "返出批准",
            "planned_departure_at": "2026-09-24 09:51",
            "actual_departure_at": "",
            "sequence_no": "2955421",
            "status_url": "http://das/eGate/tm/R_EGT_GERPTMRESERVEDETAIL.aspx?Regdate=20260924&Seqno=2955421&MailSeq=ODL26091200225",
            "event_target": "dgMain$ctl03$ctl00",
        }
    ]


def test_find_cpm_id_from_hidden_field_or_url():
    assert find_cpm_id('<input id="hddCpmId" value="9012">') == "9012"
    assert (
        find_cpm_id("", "http://das.example/detail?cpm_id=9013&mode=view")
        == "9013"
    )


def test_latest_cpm_id_uses_first_result_row():
    source = """
    <table><tr><th>CPMID</th></tr>
    <tr><td><a href="V_CPM_DETAIL.aspx?cpm_id=9910">9910</a></td></tr>
    <tr><td><a href="V_CPM_DETAIL.aspx?cpm_id=9909">9909</a></td></tr></table>
    """
    assert find_latest_cpm_id(source) == "9910"


def test_parse_cpm_search_rows_extracts_metadata_and_stage():
    source = """
    <table id="dgMain"><tr><th>序号</th><th>检查序号</th></tr>
    <tr><td>1</td><td>100687</td><td>CAAU9592527</td>
      <td><a href="V_CPM_DETAIL.aspx?cpm_id=100687">1.进厂检查</a></td>
      <td>2026-09-23 08:13:34</td><td>&nbsp;</td><td>空调</td><td>整箱</td></tr>
    <tr><td>2</td><td>100686</td><td>HLHU8529743</td>
      <td><a href="V_CPM_DETAIL.aspx?cpm_id=100686">2.空箱检查</a></td>
      <td>2026-09-23 08:11:06</td><td>&nbsp;</td></tr>
    <tr><td>3</td><td>100685</td><td>CAAU5328959</td>
      <td><a href="V_CPM_DETAIL.aspx?cpm_id=100685">5.铅封确认</a></td>
      <td>2026-09-23 08:01:06</td><td>&nbsp;</td></tr></table>
    """
    rows = parse_cpm_search_rows(source)
    assert [row["cpm_id"] for row in rows] == ["100687", "100686", "100685"]
    assert rows[0]["container_no"] == "CAAU9592527"
    assert rows[0]["begin_date"] == "2026-09-23 08:13:34"
    assert rows[0]["product_type"] == "空调"
    assert rows[0]["packing_type"] == "整箱"
    assert rows[1]["business_stage"] == 2
    assert rows[1]["latest_stage_code"] == "U2"
    assert rows[2]["das_process_status"] == 5
    assert rows[2]["business_stage"] == 4
