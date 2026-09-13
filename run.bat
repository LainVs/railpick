@echo off
pip install -r requirements.txt
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
pause
