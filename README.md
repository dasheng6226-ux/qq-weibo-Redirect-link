# QQ / 微博电商转链机器人

一个运行在 Windows 上的电商内容转发工具：接收指定 QQ 群消息或监控指定微博用户，将其中的淘宝、天猫、京东等商品链接转换后，转发到指定的微信目标群。

项目提供图形化配置界面，无需直接修改代码；微博登录态从本机默认 Chrome 缓存读取，并支持在服务运行期间自动刷新 Cookie。

> 本项目仅应在你拥有授权的 QQ 群、微博账号和微信环境中使用。请遵守各平台规则、适用法律及折淘客等服务的使用条款。

## 功能

- 图形界面配置微信目标群、QQ 源群号、Webhook 端口和折淘客参数。
- QQ 与微博独立屏蔽词设置。
- 自定义文本替换规则，支持替换或删除指定文字。
- QQ 消息与微博内容自动转链并转发到微信。
- 淘宝/天猫商品链接优先生成淘口令，京东商品链接生成短链接。
- 微博 Cookie 从默认 Chrome 缓存读取，无需在工具中保存微博账号或密码。
- 微博接口检测到登录失效、请求被拒绝或异常时，后台自动启动浏览器缓存副本并尝试更新 `SUB`。
- 图形界面实时日志；日志文件按两天滚动清理。
- 可打包为便携 Windows 程序，并可附带 Chrome 与匹配的 ChromeDriver。
- 微博会刷新最新5条内容，同时也会抓取博主最新的评论。

## 运行环境

- Windows 10 / Windows 11（需要图形桌面环境）。
- Python 3.10+（运行源码时需要）。
- Google Chrome（建议安装，用于保存你的默认微博登录缓存）。
- 微信随意版本，最新版本也可，唯独需要独立聊天窗口。
- [NapCatQQ](https://github.com/NapNeko/NapCatQQ)（QQ 消息来源，使用 OneBot 11 HTTP 上报）。
- 折淘客 AppKey、SID、PID、UnionID（使用转链功能时需要）。

## 从源码运行

```powershell
git clone <你的仓库地址>
cd <仓库目录>
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python .\qq-weibo-Redirect-link.py
```

也可以直接双击 `启动转链机器人.bat`。

## 首次配置

1. 用 Google Chrome 手动登录一次 [weibo.com](https://weibo.com/)。
2. 启动程序，在“转发与屏蔽”“折淘客与微博”“文本替换”中填写必要信息。
3. 点击“保存配置”。
4. 点击“从默认 Chrome 缓存获取 SUB”。程序会复制 Chrome 默认缓存到独立浏览器副本、读取 `SUB` 并自动关闭临时浏览器。
5. 点击“启动服务”。
6. 微博默认刷新频率为5-10秒，如有个人需要，可修改time.sleep的区间。

微博登录账号和密码不会出现在本工具的界面，也不会由本工具保存。

## NapCatQQ 对接（QQ 转链必需）

本项目不包含 NapCatQQ 本体，但 QQ 消息转发依赖 NapCatQQ 的 OneBot 11 HTTP 上报功能。请单独安装并启动 NapCatQQ，再在 NapCatQQ 的 WebUI 中添加 HTTP 上报。

若 NapCatQQ 与本程序运行在同一台电脑，推荐填写：

```text
上报地址：http://127.0.0.1:5000/webhook
上报格式：OneBot 11
```

其中 `5000` 必须与工具界面的“Webhook端口”一致。若两者部署在不同机器，请将 `127.0.0.1` 替换为运行本程序的服务器局域网 IP，并在防火墙中仅放行 NapCatQQ 所在机器访问该端口。

程序仅处理 OneBot 11 的群消息事件（`post_type=message`、`message_type=group`），并且只转发界面“QQ源QQ群号”中填写的群。

## Cookie 自动刷新机制

服务运行时，每次获取微博内容失败（例如登录失效、接口拒绝、HTTP 异常）后，程序会尝试自动刷新：

1. 启动独立的 Chrome 缓存副本，不占用你日常使用的 Chrome。
2. 打开 `weibo.com` 并读取当前默认 Chrome 缓存中的完整 Cookie。
3. 读取到 `SUB` 后立即更新运行中的会话与本地配置。
4. 重新获取微博内容。

为避免频繁启动浏览器，自动刷新最多每 10 分钟尝试一次。若默认 Chrome 自身的微博登录态也已过期，请手动用 Chrome 登录一次微博；之后机器人会在下一次自动刷新时读取新缓存。

## 配置说明

| 配置项 | 说明 |
| --- | --- |
| 微信目标群名 | 接收转发内容的微信群名称。务必独立窗口 |
| QQ源QQ群号 | 接收消息的 QQ 群号，多个群号可用逗号或换行分隔。 |
| Webhook端口 | QQ 消息接收服务端口，默认 `5000`。 |
| QQ转发屏蔽词 | QQ 内容包含任一词时不转发。 |
| 微博转链屏蔽词 | 微博内容包含任一词时不转发。 |
| 微博用户UID | 要监控的微博用户 UID，支持多行输入。 |
| 折淘客参数 | AppKey、SID、PID、UnionID。请从自己的折淘客账户获取。 |
| 微博 Cookie (SUB) | 通常由“从默认 Chrome 缓存获取 SUB”自动写入；仅用于核对或紧急手动粘贴。 |
| 替换规则 | 每行一条 `原文 => 新文`；右侧留空表示删除原文。 |


## 打包为便携程序

```powershell
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --clean --windowed --onedir --name "QQ微博电商转链机器人" --collect-all selenium --hidden-import=win32timezone .\qq-weibo-Redirect-link.py
```

若需要在未安装 Chrome 的电脑上使用，可将 Chrome 程序目录和与其版本匹配的 `chromedriver.exe` 一同放入打包目录：

```text
QQ微博电商转链机器人/
├─ QQ微博电商转链机器人.exe
├─ chromedriver.exe
├─ chrome/
└─ _internal/
```

若希望读取目标电脑的既有微博登录态，目标电脑仍应安装 Google Chrome，并使用默认用户资料登录微博。



## 常见问题

### 为什么自动刷新后仍无法获取微博？

请先检查服务器 Chrome 是否仍登录 `weibo.com`。Chrome 也退出登录时，程序无法绕过验证码或二次验证，需要你手动登录一次。

### 程序会控制我的日常 Chrome 吗？

不会。程序会复制默认 Chrome 中与登录相关的缓存到独立目录后再启动浏览器，避免直接锁定日常 Chrome 的用户资料。

### 为什么新电脑没有读取到登录态？

微博 Cookie 与当前 Windows 用户、Chrome 默认资料有关。请在新电脑上用 Chrome 手动登录微博一次，然后点击“从默认 Chrome 缓存获取 SUB”。

## 免责声明

本项目按“现状”提供。使用者应自行承担配置、账号安全、平台合规和内容转发的责任。
