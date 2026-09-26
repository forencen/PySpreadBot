# PySpreadBot

Python 项目基础骨架，源码直接放在 `src/`，使用 `.env` 管理本地配置。
需要 Python 3.11 或更高版本。

## 快速开始

在项目根目录执行：

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
python -m pyspreadbot
```

安装后也可以使用 `pyspreadbot` 命令启动。当前入口仅输出启动日志，后续业务代码可添加到 `src/`。
打包配置将 `src/` 映射为 `pyspreadbot` 包，因此安装后的模块名仍为 `pyspreadbot`。

## 配置

默认读取当前工作目录下的 `.env`，已有环境变量优先于文件中的配置。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_NAME` | `PySpreadBot` | 应用名称 |
| `LOG_LEVEL` | `INFO` | 支持 DEBUG、INFO、WARNING、ERROR、CRITICAL |

`.env` 已加入 Git 忽略规则。新增配置时请同步更新 `.env.example`，不要将密码、密钥或令牌写入模板。

## 项目结构

```text
PySpreadBot/
├── src/                    # Python 源码，安装为 pyspreadbot 包
│   ├── __init__.py
│   ├── __main__.py          # 启动入口
│   └── config.py            # 配置加载
├── .env                    # 本地配置，不提交
├── .env.example            # 配置模板
├── .gitignore
├── pyproject.toml          # 项目元数据、依赖及源码映射
├── setup.cfg               # 打包元数据输出位置
├── README.md
├── .venv/                  # 本地虚拟环境，不提交
├── pyspreadbot.egg-info/    # 生成的打包元数据，不提交
├── build/                  # 构建时生成的临时目录，不提交
└── dist/                   # wheel 和源码分发包，不提交
```

## 打包

在项目根目录执行 `uv build`，生成的 wheel 和源码分发包放在 `dist/`。
构建临时文件位于 `build/`，`*.egg-info/` 打包元数据位于项目根目录，均不放入 `src/` 且不提交到 Git。
