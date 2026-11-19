# 家教雷达

Windows 本地家教订单雷达。每 5 秒只读检查选定的微信群，拆分订单并按个人画像筛选，提供固定评分、通知、候选地图和公交通勤。订单滚动保留 48 小时。

## 许可

本项目源码采用 [MIT 许可证](LICENSE)。第三方依赖适用各自的许可证。微信读取依赖 `wechatauto-replica==1.1.9` 的包元数据标注为 Apache-2.0。源码包仅包含依赖声明，安装时单独获取依赖。高德地图服务适用其服务条款。

## 下载后怎么用

- [安装指南](docs/安装指南.md)介绍 Windows 安装、首次填写条件、选择群聊和确认监控。
- [使用指南](docs/使用指南.md)介绍候选、地图、模型、常见问题和卸载。

## 开发预览

需要 Windows、已登录的电脑版微信、Python 3.11+ 和 Node.js 22.13+。微信读取曾在 4.1.12.55 上验证，其他版本需要自行验证。运行 Tauri 窗口还需要 Rust MSVC、Visual Studio C++ Build Tools 和 WebView2。

在源码目录用 PowerShell 执行：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m pip install --no-deps wechatauto-replica==1.1.9
.\.venv\Scripts\python.exe -m pip install "cryptography>=41,<51" "psutil>=5.9,<8" "zstandard>=0.23,<1"
Copy-Item config.example.toml config.toml
```

编辑 `config.toml`，填入微信数据目录和初始群聊 ID。可用 `tutor-monitor probe sessions --help` 查看群聊发现命令。通过 `[profile]` 设置性别、学校、年级、可教科目和周末限制，并按需要修改 `[scoring.capability]` 的经验示例。

```powershell
cd dashboard
npm ci
npm run tauri dev
```

源码开发模式从项目目录启动 Python 后台。要构建独立安装包，构建环境需额外安装 PyInstaller，并从源码根目录运行 `./build-windows.ps1`。脚本检查发布专用地图配置、打包 Python 后台并调用 Tauri 生成 NSIS 安装程序。安装后的本地数据保存在当前用户的应用数据目录。

独立安装版首次启动时填写个人条件和出发地，然后使用「管理群聊」选择已登录微信中的群。基础筛单和固定评分无需模型。「工具 → 本地模型（可选）」会引导选择已有的 Ollama 模型目录（包含 blobs 和 manifests），或选择一个新目录。检测和下载均使用所选目录，下载前会显示完整路径并要求确认。没有安装 Ollama 时，向导可打开官方 Windows 下载页，安装完成后回来重新检测。准备完成后，运行中的雷达会在下一轮检查时启用模型。

之后可从窗口顶部的「我的条件」修改画像和出发地。保存后下一轮监控会用新条件处理新订单，既有候选仍保留当时的筛选结果。模型入口在「工具」菜单。

## 地图、模型与数据

内测者可在实时雷达顶部点击「导入内测地图配置」，选择维护者私发的 JSON 文件。应用先用两个深圳公共地标试算公交，试算通过后保存配置，并自动打开地图显示底图加载结果。后台通常会在下一轮通勤补查时读取新配置。维护者可参照 `amap-beta.example.json` 创建 `amap-beta.json`，填入专用的三项凭据后单独私发。

导入后的凭据以明文保存在本机 `data/private/amap-beta.json`。测试者可以读取，内测结束后可在高德平台停用专用 Key。

源码开发模式的地图和本地模型默认关闭。独立安装版会预置发布专用高德配置，用户可直接使用地图。高德会收到地理编码和通勤计算所需的位置数据。发布包的高德凭据可被提取，请使用可单独轮换的专用凭据。不要提交填好的配置文件或构建目录。

微信只读访问。本地订单、微信密钥缓存和运行数据应保留在本机。公开截图或示例时请使用虚构订单，避免暴露群聊成员、学生地址和联系方式。
