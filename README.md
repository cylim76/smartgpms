# smartGPMS

可在 Windows 或 Linux 运行的 DAS 门证、监装照片、箱号和铅封号核验工具。

## 当前能力

- 一次核验多个集装箱，后台同步 DAS 门证、监装照片和 PDF；
- RapidOCR + ONNX Runtime CPU 本地识别，无需大型 AI 镜像；
- DAS 查询/下载保持单浏览器串行，照片扫描每 20 箱检查门证调度，OCR 使用独立单 CPU 工作线程；
- SQLite 保存业务数据、OCR 缓存、PDF 路径和打印历史；
- Windows 使用当前用户的 DPAPI 加密凭据；
- Linux 使用权限为 `0600` 的本机 Fernet 密钥加密凭据；
- OTP 永不保存；
- 后台浏览器在 Windows 优先使用 Edge，在 Linux 使用 Playwright Chromium；
- 业务日期和后台任务时间统一使用中国标准时间（UTC+8），不受 Linux 主机时区影响；
- 服务端生成门证 PDF，最终打印由访问页面的客户端电脑完成。

完整方案见 [docs/development_plan.md](docs/development_plan.md)。

## Windows 本机版

1. 首次双击 `setup_win.bat`。
2. 以后双击 `run_win.bat`。
3. 系统以独立应用窗口打开，不显示普通浏览器标签栏和地址栏。

关闭该独立窗口后，smartGPMS 会停止接收新任务，完成当前正在处理的箱号，随后关闭 DAS/SSO 后台会话和本机服务。尚未开始的队列会保留到下次启动。

Windows 的操作方式与原版本保持一致。检测到 Microsoft Edge 时直接复用 Edge；没有 Edge 时安装 Playwright Chromium。

## Windows 服务版

1. 先运行一次 `setup_win.bat`；
2. 以管理员权限运行 `install_service.bat`；
3. 安装完成后，服务自动启动并随 Windows 延迟启动；内网客户端访问 `http://服务器IP:8765`；
4. 不再使用服务模式时，以管理员权限运行 `uninstall_service.bat`。

Windows 服务由固定版本的 WinSW 托管，不显示命令行或业务窗口。安装脚本校验 WinSW SHA-256，并只为“域/专用网络”添加 TCP 8765 入站规则。客户端关闭浏览器不会停止服务。

```console
sc query smartGPMS
sc start smartGPMS
sc stop smartGPMS
```

服务默认使用 Windows `LocalSystem` 身份，因此服务模式与桌面模式的 DPAPI 密码不能互相解密。首次切换到服务模式时，需要在页面中重新登录并保存一次密码；OTP 仍不保存。卸载服务不会删除 SQLite、照片、门证 PDF、日志或本机运行环境。

## Linux systemd 服务版

推荐使用 Python 3.10 以上的 Debian/Ubuntu x86_64 环境：

```bash
chmod +x setup_linux.sh
./setup_linux.sh
sudo systemctl start smartgpms
```

`setup_linux.sh` 可从任意安装目录运行，并自动完成以下工作：

- 在 Debian/Ubuntu 上通过 `sudo` 安装与当前 Python 版本匹配的 `venv`、基础工具和中文字体；
- 自动修复上次安装失败遗留的不完整 `.venv`；
- 安装 Python 依赖、Playwright Chromium 及其 Linux 系统依赖。
- 根据当前项目绝对路径和当前 Linux 用户生成 systemd unit；
- 注册并启用 `smartgpms.service`，重启服务器后自动启动。

不要使用 `sudo ./setup_linux.sh`；安装脚本会在需要系统权限时自行调用 `sudo`。服务默认监听 `0.0.0.0:8765`，数据默认保存在项目的 `data` 目录。可在安装前通过 `SMARTGPMS_HOST`、`SMARTGPMS_PORT`、`SMARTGPMS_DATA_DIR` 修改这些值。其他 Linux 发行版需要先使用本机软件包管理器安装 Python 的 `venv/ensurepip`，随后仍可运行同一个安装脚本。

常用管理命令：

```console
sudo systemctl start smartgpms
sudo systemctl stop smartgpms
sudo systemctl restart smartgpms
systemctl status smartgpms
journalctl -u smartgpms -f
```

卸载或移动项目目录前，应先执行 `sudo systemctl disable --now smartgpms`。重新运行 `setup_linux.sh` 会按照当前安装目录和环境变量更新服务配置。

服务器入口默认监听 `0.0.0.0:8765`，并且固定使用一个 Uvicorn worker。当前版本仍是单 SSO 会话架构，只能部署在受信任内网；正式开放给多个客户端前，应配置防火墙白名单、HTTPS/反向代理和应用访问认证。

服务器模式没有业务窗口生命周期。Windows、Linux 或其他客户端关闭浏览器，只会关闭自己的页面，不会停止服务端；服务端应通过 Windows 服务管理器、`systemd` 或启动它的终端进行停止和重启。

Linux 安装脚本使用 [deploy/smartgpms.service](deploy/smartgpms.service) 动态生成实际 unit。生产环境建议：

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
