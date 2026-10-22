@echo off
REM offline regression, no network: cache/group/graph/cancel/offline/display/frontend/desktop smoke
cd /d %~dp0..
python tests\test_cache.py || exit /b 1
python tests\test_group.py || exit /b 1
python tests\test_graph.py || exit /b 1
python tests\test_cancel.py || exit /b 1
python tests\test_offline.py || exit /b 1
python tests\test_p0_regress.py || exit /b 1
python tests\test_archive.py || exit /b 1
python tests\test_throttle.py || exit /b 1
python tests\test_display.py || exit /b 1
python tests\test_frontend.py || exit /b 1
python desktop.py --smoke || exit /b 1
echo ALL OFFLINE TESTS PASSED
pause
