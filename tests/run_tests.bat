@echo off
REM offline regression, no network: cache/group/graph/cancel/desktop smoke
cd /d %~dp0..
python tests\test_cache.py || exit /b 1
python tests\test_group.py || exit /b 1
python tests\test_graph.py || exit /b 1
python tests\test_cancel.py || exit /b 1
python desktop.py --smoke || exit /b 1
echo ALL OFFLINE TESTS PASSED
pause
