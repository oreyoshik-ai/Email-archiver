# 邮件归档管理系统

把客户发到 163（及 QQ、Gmail、Outlook 等）邮箱的邮件，按「**日期 → 客户邮箱 → 每一封**」自动整理到本地文件夹。纯 Python 标准库实现，**零依赖**，下载即用。

每一封邮件落盘时会同时生成：邮件预览截图（PNG）、附件清单（含压缩包自动解压）、隐藏的元数据 `meta.json`，并在当天目录生成一份 `_日报.csv` 汇总清单。

---

## 功能特点

- **一键归档**：填邮箱 + 授权码，点按钮即可整理指定日期的邮件
- **结构清晰**：`归档目录 / 日期 / 发件人 / 主题_序号 /`，同一客户多封信自动编号
- **去重**：基于 Message-ID 全局索引，重复运行不会重复保存
- **附件自动解压**：zip / tar / rar / 7z 自动展开，且拦截路径穿越（防 `../../` 逃逸）
- **邮件预览**：调用本机 Edge/Chrome 无头模式，把邮件正文渲染成 PNG 截图
- **内嵌图片**：正文里 `cid:` 引用的图片会正确嵌进截图
- **日报**：每个日期目录下一份 `_日报.csv`，记录当天所有邮件
- **Web 操作页**：内置单页前端，可视化操作，无需命令行
- **多邮箱支持**：自动识别 163/126/yeah/qq/foxmail/gmail/outlook/hotmail/live 的 IMAP 服务器
- **本地导入**：支持从 `.eml` / `.mbox` 文件导入，不连邮箱也能用
- **安全**：配置文件在 gitignore 中，授权码不进版本库

---

## 环境要求

- **Python 3.10 或更高版本**（下载地址：<https://www.python.org/downloads/>，安装时勾选 "Add Python to PATH"）
- 本机装有 **Edge** 或 **Chrome** 浏览器（用于生成邮件预览截图；没有也能用，只是不生成截图）
- Windows / macOS / Linux 均可

---

## 快速开始

### 方式一：Windows 双击启动

1. 安装 Python 3.10+
2. 双击 `邮件归档.bat`，**不要关闭弹出的黑窗口**
3. 浏览器会自动打开页面；没自动打开时访问 <http://127.0.0.1:8765/>
4. 在页面里填 163 邮箱和授权码，点「开始整理这一天的邮件」

### 方式二：命令行启动

```bash
python -m mail_archiver              # 打开网页（默认）
python -m mail_archiver --no-browser # 启动但不自动开浏览器
python -m mail_archiver --port 9000  # 指定端口
```

### 方式三：纯命令行归档（不开网页）

```bash
python -m mail_archiver --cli --date 2026-09-18          # 归档指定日期
python -m mail_archiver --from-dir /path/to/emls        # 从目录导入
python -m mail_archiver --from-file /path/to/mail.eml   # 导入单个文件
```

### 第一次用？先跑自检

```bash
python -m mail_archiver --self-test
```

不连邮箱，用内置 5 封样例邮件走通完整归档流程，验证本机环境是否正常。

---

## 授权码怎么拿（163 为例）

授权码**不是邮箱登录密码**。获取步骤：

1. 浏览器打开 <https://mail.163.com> 并登录
2. 进入「**设置**」→「**POP3/SMTP/IMAP**」
3. 开启 **IMAP/SMTP** 服务
4. 点「**生成授权码**」，按提示发短信验证
5. 把生成的一串字符粘贴到本工具页面

网页操作页里也有这几步说明。授权码只需填一次，会保存到 `config.local.json`，除非重新生成了授权码，否则不用再填。

> QQ 邮箱、Gmail 等流程类似：在各自邮箱设置里开启 IMAP 并生成授权码 / 应用专用密码。

---

## 归档目录结构

默认保存到桌面「邮件归档」文件夹，可在页面修改。结构示例：

```
邮件归档/
└── 2026-09-16/
    ├── _日报.csv                          ← 当天汇总清单
    ├── 张三zhangsan@example.com/
    │   ├── 无法登录生产环境_001/
    │   │   ├── 邮件预览.png               ← 邮件正文截图
    │   │   ├── meta.json                 ← 元数据（隐藏文件）
    │   │   └── attachments/
    │   │       ├── 截图.png
    │   │       └── 日志.zip → 自动解压到 日志/
    │   └── 发票请尽快开_002/
    └── 李四lisi@example.com/
        └── 压缩包路径测试_001/
```

每封邮件一个独立子文件夹，**同一发件人当天多封信按 `_001`、`_002` 顺序编号**。`meta.json` 记录主题、发件人、时间、附件清单等元数据。

---

## 配置

配置文件 `config.local.json`（首次运行自动生成，已加入 `.gitignore`，不进版本库）：

```json
{
  "archive_root": "D:\\邮件归档",
  "timezone": "Asia/Shanghai",
  "imap": {
    "enabled": true,
    "host": "imap.163.com",
    "port": 993,
    "ssl": true,
    "username": "name@163.com",
    "auth_code": "你的授权码",
    "folder": "INBOX"
  },
  "extract": {
    "enabled": true,
    "max_files": 500,
    "max_bytes": 524288000
  }
}
```

也支持环境变量覆盖（优先级最高，方便容器 / CI 使用）：

| 环境变量 | 作用 |
|---|---|
| `COMPLAINT_ARCHIVE_ROOT` | 归档根目录 |
| `COMPLAINT_IMAP_HOST` | IMAP 主机 |
| `COMPLAINT_IMAP_PORT` | IMAP 端口 |
| `COMPLAINT_IMAP_USER` | 邮箱账号 |
| `COMPLAINT_IMAP_AUTH` | 授权码 |
| `COMPLAINT_IMAP_FOLDER` | 收件夹（默认 INBOX） |

生成空白配置：`python -m mail_archiver --init-config`

---

## 命令行参数一览

| 参数 | 作用 |
|---|---|
| `--web` | 打开网页（默认行为，可不加） |
| `--cli` | 使用命令行，不打开页面 |
| `--port 8765` | 网页端口 |
| `--no-browser` | 启动网页但不自动开浏览器 |
| `--date YYYY-MM-DD` | 归档日期（命令行模式） |
| `--from-dir <目录>` | 从目录导入 .eml / .mbox |
| `--from-file <文件>` | 导入单个 .eml 或 .mbox |
| `--init-config` | 生成空白 config.local.json |
| `--self-test` | 运行内置自检 |
| `-v` / `--verbose` | 详细日志 |
| `--version` | 显示版本 |

---

## 支持的邮箱

工具会根据邮箱后缀自动推断 IMAP 服务器：

| 邮箱后缀 | IMAP 服务器 |
|---|---|
| 163.com | imap.163.com |
| 126.com | imap.126.com |
| yeah.net | imap.yeah.net |
| qq.com / foxmail.com | imap.qq.com |
| gmail.com | imap.gmail.com |
| outlook.com / hotmail.com / live.com | outlook.office365.com |

其它邮箱可在配置文件里手动填写 `host`。

---

## 项目结构

```
mail_archiver/
├── __main__.py      ← 入口
├── cli.py           ← 命令行参数解析
├── config.py        ← 配置读写、邮箱域名推断
├── pipeline.py      ← 归档调度（IMAP / 本地 / 演示）
├── imap_client.py   ← IMAP 连接、按日期搜索拉取
├── parse.py         ← 邮件解析（主题/发件人/正文/附件）
├── archiver.py      ← 核心归档：目录生成、去重、落盘
├── index.py         ← Message-ID 去重索引
├── extract.py       ← 压缩包解压与路径穿越防护
├── report.py        ← 日报 CSV 生成
├── screenshot.py    ← Edge/Chrome 无头截图
├── local_import.py  ← 本地 eml/mbox 导入
├── results.py       ← 已归档结果列表（供 Web 展示）
├── jobs.py          ← 后台任务运行器
├── webapp.py        ← 内置 HTTP 服务 + REST API
├── util.py          ← 工具函数
├── selftest.py      ← 内置自检
└── web/index.html   ← 单页前端
```

详细说明见 [项目维护文档.md](项目维护文档.md)。

---

## 开发与贡献

- 零外部依赖，全部使用 Python 标准库
- 自检命令：`python -m mail_archiver --self-test`
- 修改代码后请确保自检通过

---

## 许可证

本项目暂未声明开源许可证。如需使用或二次开发，请联系作者。
