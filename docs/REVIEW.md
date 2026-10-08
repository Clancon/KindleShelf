# v0.2.0 审查与验证记录

本次发布统一 Windows、Linux 和单容器部署入口，保留 schema 1 和已有数据。审查覆盖数据库与文件操作、文章抓取与转换、HTTP 表单与下载、部署配置及发布边界。

## 修复的实际问题

| 范围 | 问题与修复 |
| --- | --- |
| 阅读状态 | 已完成内容无法可靠重置未读；电脑重读表单残留 100%。现在按新一轮阅读更新状态和时间，保留此前历史。 |
| SQLite | 连接未关闭，并发状态变更可能产生不一致事件链。现在保证关闭连接，并在读取旧状态前获取写事务。 |
| 文件与迁移 | 失败的文件操作可能留下半成品，非法旧 JSON 可能部分迁入。现在验证路径与记录，原子迁移并回滚文件变更。 |
| 目录边界 | 文件和目录链接可能逃出数据目录。书目路径、索引和下载增加链接及路径边界检查。 |
| 备份 | 目标覆盖活动数据库或失败写入可能破坏数据。现在使用 SQLite 在线备份和临时文件原子替换，拒绝活动数据库及关联文件。 |
| 下载统计 | HEAD、304、Range 和失败请求曾可能被算作下载。现在仅统计完整 HTTP 200 GET；不保证识别传输中断。 |
| 历史 | 固定条数无法访问完整记录；过大游标会产生服务器错误。现在支持游标分页、完整流式导出和 SQLite 整数范围校验。 |
| 抓取 | 页面、重定向及图片 URL 可访问私网或特殊地址。现在验证全部 DNS 地址、固定连接 IP，并保留 TLS 域名和证书验证。 |
| HTML | 根节点属性和资源链接可能保留活动内容或访问本地文件。现在清理危险标签、属性、资源和 CSS，限制源目录内栅格图片。 |
| 转换 | 转换失败可能覆盖目标文件或暴露原始路径和错误输出。现在先生成临时结果，成功后替换，并提供通用错误提示。 |
| HTTP 安全 | 固定会话密钥、写表单缺少 CSRF、任意 Host 及私人响应缓存。现在随机持久密钥、逐表单校验、可信 Host 和禁止私人响应缓存。 |
| 部署 | 数据目录所有权、实际访问地址与重启行为未明确。现在非 root 运行、显式准备绑定目录、配置公网/局域网 URL、健康检查和重启策略。 |
| 依赖 | 审计发现 Werkzeug 3.1.8 的已知漏洞。已升级到 3.1.9，并重新生成精确版本和哈希锁。 |

## 本机验证

环境为 Windows、Python 3.10，安装了实际 Calibre。自动化测试使用临时目录，不读取私人书籍；运行数据和日志不属于发布内容。

| 检查 | 结果 |
| --- | --- |
| 完整 pytest 回归 | **185 passed，0 skipped** |
| Ruff 代码检查 | 通过 |
| Ruff 格式检查 | 27 个 Python 文件均通过 |
| `pip check` | No broken requirements found |
| 哈希锁依赖安装与 `setup.ps1` | 成功 |
| 运行依赖 `pip-audit` | No known vulnerabilities found |
| 实际公网 HTTPS 抓取 | example.com 获取并清理正文成功 |
| 真实 Calibre | 生成 MOBI，检查 `BOOKMOBI` 文件头；端到端转换与下载通过 |
| 实际 Waitress HTTP 服务 | 就绪与版本、CSRF 表单、生成并下载 MOBI、重读重置、统计、完整历史和一致备份通过 |
| HTTP 服务进程重启 | 书目状态、历史和 MOBI 内容保持一致 |

完整回归命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -q --junitxml=regression-results.xml
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pip_audit --require-hashes --disable-pip -r requirements.txt
```

本机依赖审计曾遇到继承的代理 TLS 故障，最终仅在该审计进程中对 `pypi.org,api.osv.dev` 设置 `NO_PROXY` 后成功，没有修改系统代理配置。审计结果仅代表当时漏洞数据库中的已知 Python 依赖问题，不涵盖系统软件和所有未知漏洞。

## Linux 与容器验证

本机没有 Docker/Podman，未执行真实 Linux 或容器运行测试。配置契约和健康检查行为测试已通过，但不能以此代替镜像构建和容器执行。

GitHub Actions 为每次代码推送配置了以下实际验证：

- Windows/Ubuntu × Python 3.10/3.12：安装真实 Calibre，运行 Ruff 和完整回归。
- 运行依赖审计。
- Linux Docker：验证 Compose，构建镜像，检查构建上下文排除私人文件、非 root 运行、资源限制、真实转换下载、阅读历史、停止和重启持久化。

每次发布的 Linux/容器结论以对应提交的 Actions 结果为准。Docker 检查脚本仅使用临时源目录和数据，不挂载本机真实书架。

## 发布与保留的边界

发布内容是代码、无密钥配置样例、依赖锁、测试、CI 和文档。私人数据目录、书籍、数据库、日志、备份、`.env`、密钥和虚拟环境由 Git 忽略；Docker 构建上下文使用运行文件白名单。`scripts/check_publish.py` 检查暂存区，只报告问题文件名，不输出凭据值。发布前仍需人工核对暂存清单。

以下范围未作通过承诺：

- 第七代 Kindle 真机的下载、Cookie 和排版表现。
- 原厂 Kindle 本地书籍阅读进度自动同步；当前仅支持手动更新。
- 绕过微信公众号的登录或验证页；当前拒绝此类页面。
- 针对任意恶意 EPUB/DOCX 或图像的完整沙箱；解析仍依赖 Calibre。
- DNS 解析的严格总超时；操作系统解析器可能比抓取预算耗时更长。
- 不可信网络中的用户认证；本服务仍是可信局域网内的单用户工具。

部署、迁移和恢复步骤见 [README.md](../README.md)，安全边界见 [SECURITY.md](../SECURITY.md)。
