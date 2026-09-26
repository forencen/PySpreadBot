# PySpreadBot

Python 项目基础骨架，源码放在 `src/pyspreadbot/`，使用 `.env` 管理本地配置。
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

安装后也可以使用 `pyspreadbot` 命令启动。当前入口仅输出启动日志，后续业务代码可添加到 `src/pyspreadbot/`。

## 配置

默认读取当前工作目录下的 `.env`，已有环境变量优先于文件中的配置。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_NAME` | `PySpreadBot` | 应用名称 |
| `LOG_LEVEL` | `INFO` | 支持 DEBUG、INFO、WARNING、ERROR、CRITICAL |

`.env` 已加入 Git 忽略规则。新增配置时请同步更新 `.env.example`，不要将密码、密钥或令牌写入模板。

## 项目结构

```text
src/pyspreadbot/
  __init__.py
  __main__.py    # 启动入口
  config.py      # 配置加载
.env.example     # 可提交的配置模板
pyproject.toml   # 项目元数据、依赖和打包配置
```
