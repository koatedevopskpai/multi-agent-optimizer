# Delivery Recovery Assistant UI shell

Minimal React/TypeScript shell for the deterministic recovery API. It sends the
selected synthetic scenario to `POST /v1/recovery/evaluations` and displays the
returned facts. It does not calculate savings, costs or recommendations.

## Run locally

From the repository root:

```powershell
.\.venv-focused\Scripts\python.exe -m uvicorn delivery_recovery.api.app:app --host 127.0.0.1 --port 8000
```

In another terminal:

```powershell
cd web
npm install
npm run dev
```

Open the Vite URL (normally `http://127.0.0.1:5173/ui/`). Vite proxies `/v1` to
the API on port 8000.

Run the interaction tests with:

```powershell
cd web
npm test
```

The tests cover budget-change stale-result invalidation and ignoring a
superseded response after a scenario switch.

## Build and serve from FastAPI

```powershell
cd web
npm install
npm run build
cd ..
.\.venv-focused\Scripts\python.exe -m uvicorn delivery_recovery.api.app:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/ui/`. The API also exposes `/docs` and `/healthz`.

## Scope

- Synthetic scenario selector and budget input.
- Baseline/disrupted cards, candidate table, rejection reasons and recommendation.
- Explicit labels for feasible/rejected status (not colour-only).
- No model narrative, approval, persistence or external project-system update.

Screenshots and visual verification are owned by Codex. This shell has been
build-checked, not browser-captured by OpenCode.
