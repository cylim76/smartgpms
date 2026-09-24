# smartGPMS

Windows 本机运行的 DAS 门证箱号/铅封号核验与打印工具。

## 当前能力

- 左侧一次输入多个箱号，右侧同时显示约 6 行核验结果；
- 独立后台 Edge 会话完成 SSO（用户名、密码、OTP）及 DAS 登录；
- 用户名和密码使用当前 Windows 用户的 DPAPI 加密保存，OTP 不保存；
- SQLite 保存 CPMID/箱号/业务阶段、OCR 缓存、打印状态及不可变打印历史；
- DAS 照片页面的流程状态转为 `5.铅封确认` 时，排队下载 U1/U2/U3/S1 四阶段照片并预裁剪；
- RapidOCR + ONNX Runtime CPU 本地识别，自动尝试 0/90/180/270 度；
- 绿色/红色/橙色区分一致、不一致和待人工确认；
- 调用 DAS 原打印按钮成功后，将 `print_status` 从 0 改为 1，并记录最后打印时间；
- 多 CPMID 时选取最新有效记录，列表回发目标从实际 `__doPostBack` 动态读取。

完整方案见 [docs/development_plan.md](docs/development_plan.md)。

## 启动

1. 首次运行 `setup.bat`。
2. 运行 `run.bat`。
3. 系统会以独立应用窗口打开 `http://127.0.0.1:8765`，不显示普通浏览器的标签栏和地址栏。

若电脑上找不到 Edge 或 Chrome，启动程序会退回到系统默认浏览器。

系统优先使用电脑已安装的 Microsoft Edge，不会安装几十 GB 的 AI 镜像。首次 OCR 会在本机加载 ONNX 小模型。

## 尚需现场确认

DAS 是内部 ASP.NET 系统，查询框、详情弹窗和打印按钮的最终控件 ID 必须在真实登录页面做一次采集和实机打印验证。代码已有动态发现与安全失败机制：无法确认控件时不会误点其它按钮。
