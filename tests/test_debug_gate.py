"""单进程 Gate 调试入口的启动、只读模式及资源清理验证。"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from pyspreadbot.config import Settings, load_settings
from pyspreadbot.debug_gate import debug_gate
from pyspreadbot.models import Book, D, Level


@pytest.mark.parametrize("script", ["src/exchange/gate.py", "src/debug_gate.py"])
def test_direct_script_imports_from_other_directory(script, tmp_path):
    """模拟 PyCharm 用文件路径启动，并且工作目录不是项目根目录。"""
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / script), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "--contracts-only" in result.stdout


@pytest.mark.parametrize("contracts_only", [True, False])
async def test_debug_initializes_and_closes_without_trading(contracts_only):
    """即使传入 live 配置，调试入口也必须以 observe 初始化，退出时关闭。"""
    venue = Mock(initialize=AsyncMock(), connect=AsyncMock(), subscribe_depth=AsyncMock(), close=AsyncMock())
    venue.instruments = {"BTC": object()}
    venue.books = {"BTC": Book((Level(D(99), D(1)),), (Level(D(100), D(1)),))}
    factory = Mock(return_value=venue)
    await debug_gate(Settings(mode="live"), once=True, contracts_only=contracts_only, exchange_type=factory)
    assert factory.call_args.args[0].mode == "observe"
    venue.initialize.assert_awaited_once()
    venue.close.assert_awaited_once()
    if contracts_only:
        venue.connect.assert_not_called()
    else:
        venue.connect.assert_awaited_once_with({"BTC"})
        venue.subscribe_depth.assert_awaited_once_with("BTC")


async def test_debug_initialization_failure_closes():
    """HTTP 初始化失败时也必须清理已经创建的会话。"""
    venue = Mock(initialize=AsyncMock(side_effect=RuntimeError("offline")), close=AsyncMock())
    with pytest.raises(RuntimeError, match="offline"):
        await debug_gate(Settings(), exchange_type=Mock(return_value=venue))
    venue.close.assert_awaited_once()


def test_observe_override_accepts_single_exchange_without_keys(monkeypatch, tmp_path):
    """调试时只启用 Gate，无需实盘密钥；普通实盘/模拟套利仍要求两个所。"""
    monkeypatch.setenv("MODE", "live")
    monkeypatch.setenv("EXCHANGES", "gate")
    assert load_settings(tmp_path / "missing.env", mode="observe").exchanges == ("gate",)
    with pytest.raises(ValueError, match="At least 2"):
        load_settings(tmp_path / "missing.env")
