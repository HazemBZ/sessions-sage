.PHONY: run run-background stop

# Single command: starts Ollama (if needed) + uvicorn
run:
	@echo "==> Checking Ollama..."
	@if ! pgrep -x ollama > /dev/null; then \
		echo "==> Starting Ollama..."; \
		ollama serve &>/tmp/ollama.log & \
		sleep 3; \
	fi
	@echo "==> Ensuring gemma3:4b model..."
	@ollama pull gemma3:4b 2>/dev/null || true
	@echo "==> Starting sessions-sage on :8099..."
	uv run uvicorn app.main:app --host 0.0.0.0 --port 8099

# Run in background (survives terminal close)
run-background:
	@echo "==> Checking Ollama..."
	@if ! pgrep -x ollama > /dev/null; then \
		echo "==> Starting Ollama..."; \
		nohup ollama serve &>/tmp/ollama.log & \
		sleep 3; \
	fi
	@echo "==> Ensuring gemma3:4b model..."
	@ollama pull gemma3:4b 2>/dev/null || true
	@echo "==> Starting sessions-sage in background..."
	nohup uv run uvicorn app.main:app --host 0.0.0.0 --port 8099 > /tmp/sessions-sage.log 2>&1 &
	@echo "PID: $$!"
	@sleep 2
	@echo "==> Ready at http://localhost:8099"

# Stop background server
stop:
	-pkill -f "sessions-sage" 2>/dev/null; echo "stopped"
