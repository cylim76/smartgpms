# smartGPMS

可在 Windows 或 Linux 运行的 DAS 门证、监装照片、箱号和铅封号核验工具。

## 当前能力

- 一次核验多个集装箱，后台同步 DAS 门证、监装照片和 PDF；
- RapidOCR + ONNX Runtime CPU 本地识别，无需大型 AI 镜像；
- SQLite 保存业务数据、OCR 缓存、PDF 路径和打印历史；
- Windows 使用当前用户的 DPAPI 加密凭据；
- Linux 使用权限为 `0600` 的本机 Fernet 密钥加密凭据；
- OTP 永不保存；
- 后台浏览器在 Windows 优先使用 Edge，在 Linux 使用 Playwright Chromium；
- 业务日期和后台任务时间统一使用中国标准时间（UTC+8），不受 Linux 主机时区影响；
- 服务端生成门证 PDF，最终打印由访问页面的客户端电脑完成。

完整方案见 [docs/development_plan.md](docs/development_plan.md)。

## Windows 本机版

1. 首次双击 `setup.bat`。
2. 以后双击 `run.bat`。
3. 系统以独立应用窗口打开，不显示普通浏览器标签栏和地址栏。

Windows 的操作方式与原版本保持一致。检测到 Microsoft Edge 时直接复用 Edge；没有 Edge 时安装 Playwright Chromium。

## Linux 本机版

推荐使用 Python 3.10 以上的 Debian/Ubuntu x86_64 环境：

```bash
chmod +x setup.sh run.sh run_server.sh
./setup.sh
./run.sh
```

`setup.sh` 可从任意安装目录运行，并自动完成以下工作：

- 在 Debian/Ubuntu 上通过 `sudo` 安装与当前 Python 版本匹配的 `venv`、基础工具和中文字体；
- 自动修复上次安装失败遗留的不完整 `.venv`；
- 安装 Python 依赖、Playwright Chromium 及其 Linux 系统依赖。

有图形桌面时，`run.sh` 会尝试打开应用窗口；无桌面服务器建议直接运行 `run_server.sh`。其他 Linux 发行版需要先使用本机软件包管理器安装 Python 的 `venv/ensurepip`，随后仍可运行同一个 `setup.sh`。

## Linux/Windows 无界面服务端

Linux：

```bash
SMARTGPMS_DATA_DIR=/var/lib/smartgpms ./run_server.sh
```

Windows：运行 `run_server.bat`。

服务端入口默认监听 `0.0.0.0:8765`，并且固定使用一个 Uvicorn worker。当前版本仍是单 SSO 会话架构，只能部署在受信任内网；正式开放给多个客户端前，应配置防火墙白名单、HTTPS/反向代理和应用访问认证。

仓库提供了 [deploy/smartgpms.service](deploy/smartgpms.service) 作为 systemd 模板。生产环境建议：

- 程序安装在 `/opt/smartgpms`；
- 数据保存在 `/var/lib/smartgpms`；
- 由 Nginx 或其他反向代理提供 HTTPS；
- 服务端必须能访问公司 SSO、DAS、DNS及相关内部网络。

## 环境变量

- `SMARTGPMS_DATA_DIR`：SQLite、照片、门证 PDF、凭据和浏览器配置目录；默认是项目下的 `data`。
- `SMARTGPMS_HOST`：监听地址。
- `SMARTGPMS_PORT`：监听端口，默认 `8765`。
- `SMARTGPMS_UI_URL`：本机应用窗口打开的地址。

Windows DPAPI 文件不能直接在 Linux 解密。迁移数据目录后会保留用户名，但需要重新输入密码和 OTP，并由 Linux 重新加密保存。
