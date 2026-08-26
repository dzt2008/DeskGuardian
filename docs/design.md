```md
# DeskGuardian 项目实现书

## 1. 项目概述
- **项目名称**：DeskGuardian —— 本地课业守护助手
- **目标用户**：在校大学生，需管理课程表与作业截止日期
- **核心价值**：粘贴教务系统文本 → 自动解析（AI + 规则） → 本地存储 → 定时弹窗提醒
- **交互方式**：提供 **CLI 命令行** + **Web 仪表板** 双模式。日常使用无界面（后台静默 + 系统弹窗），配置管理通过浏览器访问 `http://localhost:8080` 完成。
- **运行环境**：Windows / macOS / Linux，Python 3.9+


## 2. 功能需求

### 2.1 课程管理
- 支持从教务系统复制任意格式的课表文本（含表格、Markdown、中英文混杂）
- 通过 AI（本地 Ollama + Qwen2.5-0.5B）解析为结构化数据（课程名、星期、节次、教室）
- 解析后展示预览表格，用户可全选确认、单条修改或全部重来
- 同时提供纯规则解析（正则 + dateparser）作为 AI 降级
- 终极兜底：手工逐条录入

### 2.2 作业管理
- 录入作业名称、科目、截止日期（支持“后天”、“2026-09-01”等自然语言）
- 到期前 1 天及当天 08:00 各提醒一次

### 2.3 定时提醒
- 后台进程每分钟扫描一次数据库
- 课程：当前时间在课前 5 分钟（可配置）触发弹窗
- 作业：按上述策略触发
- 每日 07:00 推送“今日课程简报”（含时间、地点）

### 2.4 配置管理
- 可调整提前提醒分钟数
- 可启用/关闭开机自启

### 2.5 Web 仪表板管理界面
- 用户通过浏览器打开本地 Web 服务，完成课表导入、数据查看、增删改查等操作
- 提供三个核心页面：
  - **导入页**：粘贴课表文本 → 调用 AI/规则解析 → 表格预览 → 单元格双击编辑 → 确认入库
  - **今日概览**：时间轴卡片展示今日课程 + 红标展示 3 天内截止作业 + 手动触发测试提醒按钮
  - **管理后台**：所有课程/作业的 CRUD 表格，支持按星期筛选、按课程名搜索、JSON 导入/导出


## 3. 系统架构（模块划分）

| 模块 | 职责 |
|------|------|
| `main.py` | CLI 入口，解析子命令（add / list / run / config / web） |
| `ai_parser.py` | 调用 Ollama，发送提示词，解析 JSON 结果 |
| `rule_parser.py` | 使用 `re` + `dateparser` + `jieba` 做规则提取 |
| `db.py` | SQLite 操作（建表、增删改查、标记已提醒） |
| `scheduler.py` | 轮询循环，比对时间触发通知 |
| `notifier.py` | 封装 `plyer` 系统通知，含蜂鸣选项 |
| `config.py` | 读写 `config.json` |
| `utils.py` | 日期解析、节次转换等辅助函数 |
| `webserver.py` | 使用 Flask 启动轻量 Web 服务，注册路由与 API 接口 |
| `templates/` | HTML 模板文件（导入页、概览页、管理页） |
| `static/` | CSS/JS 静态资源（原生 JS 构建交互表格） |


## 4. 数据流（用户视角）

### 4.1 CLI 模式
1. 用户运行 `python main.py add course`
2. 提示粘贴课表文本 → 用户 Ctrl+V 粘贴
3. 程序依次尝试：AI 解析 → 规则解析（若 AI 失败）
4. 生成预览表格（序号、课程名、星期、节次、教室）
5. 用户输入选择：全部确认 / 修改某条 / 全部重来 / 手工录入
6. 确认后写入 SQLite
7. 用户运行 `python main.py run` 启动后台守护
8. 进程每分钟扫描，匹配时弹出系统通知

### 4.2 Web 模式
1. 用户运行 `python main.py web`，Flask 在 `127.0.0.1:8080` 启动，浏览器自动打开
2. 在导入页文本框粘贴教务系统课表内容，点击“AI 解析”
3. 前端调用 `/api/parse`，后端返回结构化 JSON，页面渲染为可编辑表格
4. 用户双击表格单元格可修正 AI 识别错误
5. 点击“确认导入”，调用 `/api/import/confirm` 写入 SQLite
6. 切换至“今日概览”可查看即将开始的课程与截止作业
7. 关闭浏览器不影响后台 `scheduler` 运行


## 5. 容灾机制（四层降级）

| 层级 | 条件 | 动作 |
|------|------|------|
| 第一层 | AI 解析成功，置信度 > 0.6 | 展示预览，用户确认后入库 |
| 第二层 | AI 解析失败或置信度低 | 自动切换至规则引擎（正则 + dateparser） |
| 第三层 | 规则引擎解析结果为空 | 按行拆分文本，打印候选行，让用户手动勾选有效行 |
| 第四层 | 以上全部无效 | 进入交互式问答，逐条录入（课程名、星期、节次、教室） |

> **关键设计**：所有解析结果（无论来源）均需经过用户预览确认，杜绝脏数据入库。


## 6. 数据库设计（SQLite）

**表 `courses`**
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER | PK |
| name | TEXT | 课程名 |
| weekday | INTEGER | 1~7 |
| start_section | INTEGER | 开始节次（1~8） |
| end_section | INTEGER | 结束节次 |
| room | TEXT | 教室 |
| last_notified | TEXT | 上次提醒时间（防重复） |
| is_active | BOOLEAN | 是否启用 |

**表 `homeworks`**
| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER | PK |
| subject | TEXT | 科目 |
| content | TEXT | 作业描述 |
| deadline | TEXT | 截止日期（ISO格式） |
| notified_day1 | BOOLEAN | 提前一天是否已提醒 |
| notified_day0 | BOOLEAN | 当天是否已提醒 |

**表 `settings`**
| 字段 | 类型 | 说明 |
|------|------|------|
| key | TEXT | 配置项 |
| value | TEXT | 配置值 |


## 7. Web 仪表板接口设计

| 路由 | 方法 | 功能 |
|------|------|------|
| `/` | GET | 渲染今日概览仪表板 |
| `/import` | GET | 渲染导入页面 |
| `/manage` | GET | 渲染管理后台 |
| `/api/parse` | POST | 接收粘贴文本，调用 `ai_parser` / `rule_parser`，返回 JSON 解析结果 |
| `/api/courses` | GET | 返回所有课程 JSON（供管理表格渲染） |
| `/api/courses` | POST | 新增课程 |
| `/api/courses/<id>` | PUT | 更新单条课程（表格编辑） |
| `/api/courses/<id>` | DELETE | 删除课程 |
| `/api/homeworks` | GET / POST / PUT / DELETE | 同课程，作业的 CRUD |
| `/api/import/confirm` | POST | 接收用户确认后的课程列表，批量写入数据库 |


## 8. 技术选型

| 用途 | 依赖库 | 说明 | 是否必须 |
|------|--------|------|----------|
| 本地 AI 解析 | `ollama` + Qwen2.5-0.5B | 量化版约 600MB，断网可用 | ✅ |
| 规则解析 | `dateparser`, `jieba` | 日期解析与中文分词 | ✅ |
| 数据库 | `sqlite3`（内置） | 无额外依赖 | ✅ |
| 系统通知 | `plyer` | 跨平台原生通知 | ✅ |
| Web 框架 | `Flask` | 轻量级，渲染 HTML 模板，提供 REST API | ✅ |
| 前端交互 | 原生 JavaScript | 实现表格编辑、API 异步请求，无需 Node.js 构建 | ✅ |
| 测试 | `pytest` | 单元测试 | ✅ |
| 打包 | `PyInstaller` + UPX | 生成 `.exe` 单文件，压缩体积 | ✅ |


## 9. 项目目录结构

```

DeskGuardian/
├── main.py
├── ai_parser.py
├── rule_parser.py
├── db.py
├── scheduler.py
├── notifier.py
├── config.py
├── utils.py
├── webserver.py                # Flask 服务入口，注册 API 路由
├── config.json
├── requirements.txt
├── templates/                  # HTML 模板
│   ├── index.html              # 今日概览仪表板
│   ├── import.html             # 课表导入页面
│   └── manage.html             # 数据管理后台
├── static/                     # 前端静态资源
│   ├── style.css
│   └── app.js                  # 前端交互逻辑（表格编辑、API 调用）
├── tests/
│   ├── test_ai_parser.py
│   └── test_rule_parser.py
├── README.md
├── LICENSE
└── .github/workflows/ci.yml

```


## 10. 打包方案

### 10.1 方案选型说明

| 方案 | 优点 | 本项目选择 |
|------|------|-----------|
| **PyInstaller** | 打包体积适中（约50-80MB），支持 Flask + 模板打包，单文件 exe | ✅ **推荐** |
| Nuitka | 启动更快，体积更小（约30MB） | ❌ 编译时间长，配置复杂 |
| Docker 容器化 | 跨平台一致性高，专业感强 | ✅ **补充分发方案** |
| 不打包（源码分发） | 无额外工作量 | ❌ 用户需自行安装 Python 环境 |

### 10.2 PyInstaller 打包配置

创建 `deskguardian.spec`：

```python
# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('templates', 'templates'),
        ('static', 'static'),
        ('config.json', '.'),
    ],
    hiddenimports=[
        'flask',
        'werkzeug',
        'jinja2',
        'markupsafe',
        'click',
        'dateparser',
        'jieba'
    ],
    excludes=[
        'tkinter',
        'matplotlib',
        'pandas',
        'numpy',
        'scipy'
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='deskguardian',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
```

10.3 打包命令

```bash
# 安装
pip install pyinstaller

# 下载 UPX（可选，压缩体积）放入系统 PATH

# 执行打包
pyinstaller deskguardian.spec --clean

# 或直接命令行
pyinstaller --onefile --add-data "templates;templates" --add-data "static;static" --add-data "config.json;." --hidden-import flask --hidden-import werkzeug --hidden-import jinja2 main.py
```

10.4 打包产物

```
dist/
└── deskguardian.exe       # 单文件可执行程序（约 60-80MB）
```

用户双击后自动执行：

1. 读取/生成 config.json
2. 初始化 deskguardian.db
3. 启动 Flask 服务，监听 127.0.0.1:8080
4. 自动打开浏览器访问仪表板
5. 启动后台调度引擎

10.5 打包常见问题与解决方案

问题 原因 解决方案
exe 闪退，无法查看报错 双击运行时错误信息一闪而过 打开 CMD，手动输入 dist\deskguardian.exe 运行，错误信息会保留在控制台中
模板文件未找到（Template Not Found） .spec 中 datas 未正确包含 templates 文件夹 检查 datas=[('templates', 'templates')]；命令行打包用 --add-data "templates;templates"
静态文件 404（CSS/JS 加载失败） static 文件夹未被正确打包 确保 datas 中包含 ('static', 'static')；Flask 代码中设置 static_folder='static'
Flask / Werkzeug / Jinja2 模块缺失 PyInstaller 未自动检测到隐式导入 在 hiddenimports 中显式声明 'flask'、'werkzeug'、'jinja2'、'markupsafe'、'click'
dateparser / jieba 解析中文失败 打包后语言资源文件或词典未正确包含 在 datas 中添加语言资源路径，或在代码中捕获异常后降级到纯正则解析
exe 运行时提示“Failed to execute script” 代码中存在 __file__ 路径引用，打包后路径结构变化 使用 sys._MEIPASS 获取打包后的临时目录路径：os.path.join(sys._MEIPASS, 'templates')
中文路径或中文文件名报错 Windows 控制台编码或文件路径包含非 ASCII 字符 在代码开头设置 sys.stdout.reconfigure(encoding='utf-8')；建议用户将 exe 放在纯英文路径下运行
打包体积过大（超过 100MB） 默认打包包含了大量无关依赖 在 excludes 中排除不需要的库：'tkinter'、'matplotlib'、'pandas'、'numpy'、'scipy'；启用 upx=True 压缩
杀毒软件误报为病毒 PyInstaller 打包的 exe 常被误报 使用 upx 压缩；在 excludes 中排除不必要模块；提交给杀毒厂商加白名单；代码签名
Ollama 无法连接（AI 解析失效） Ollama 服务未启动，或地址/端口配置错误 程序内实现自动降级到规则引擎；在 config.json 中配置 ollama_url；启动时检查 Ollama 服务状态并给出友好提示

10.6 Docker 容器化（备选分发方案）

```dockerfile
FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["python", "main.py", "web"]
```

使用方式：

```bash
docker build -t deskguardian .
docker run -p 8080:8080 deskguardian
```

12. 开源规范

· 仓库：GitHub 公开仓库
· 协议：MIT License
· README：包含徽章（Python 版本、测试状态、协议）、Mermaid 架构图、5 秒快速上手命令
· CI：GitHub Actions 自动运行 pytest，确保每次提交测试通过
· 预提交：配置 pre-commit 钩子（black + flake8）
· 分发：提供 .exe 下载链接及 SHA256 校验值

13. 贡献者

感谢以下成员对本项目的贡献（排名不分先后）：


|昵称 |角色|
|冬涛觞雪| 核心开发、AI 解析模块、项目思路|
|ChatGPT-5.6-sol-xhigh| 大部分项目的编写|

```