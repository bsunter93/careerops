"""Backend validation. Reports which paths work; never prints the key."""
import os, json, subprocess, urllib.request, urllib.error
from . import fit


def _mask(k):
    return f"{k[:14]}...{k[-4:]} ({len(k)} chars)" if k and len(k) > 20 else "(malformed)"


def run() -> int:
    fit._load_env()
    key = os.environ.get("ANTHROPIC_API_KEY")

    print("KEY")
    if not key:
        print("   -- no ANTHROPIC_API_KEY (check .env)")
        return 1
    print(f"   OK {_mask(key)}")
    if not key.startswith("sk-ant-"):
        print("   !! unexpected prefix; is this the full key?")
    if "REPLACE_ME" in key:
        print("   !! placeholder still in .env")
        return 1

    print("\nAPI BACKEND  (primary)")
    body = json.dumps({"model": fit.MODEL, "max_tokens": 16,
                       "messages": [{"role": "user", "content": "Reply with exactly: ok"}]}).encode()
    req = urllib.request.Request(fit.API_BASE + "/v1/messages", data=body,
                                 headers={"content-type": "application/json", "x-api-key": key,
                                          "anthropic-version": "2023-06-01"})
    api_ok = False
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            txt = "".join(b.get("text", "") for b in json.loads(r.read().decode()).get("content", []))
        print(f"   OK {fit.API_BASE} model={fit.MODEL} -> {txt.strip()!r}")
        api_ok = True
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        print(f"   -- HTTP {e.code}")
        if e.code == 401:
            print("      key rejected: revoked, truncated, or mistyped")
        elif e.code == 400 and "credit" in detail.lower():
            print("      out of credit: add a balance at console.anthropic.com")
        elif e.code == 429:
            print("      rate limited (key itself is valid)")
        print(f"      {detail}")
    except Exception as e:
        print(f"   -- {type(e).__name__}: {e}")

    print("\nCLI BACKEND  (fallback)")
    base = os.environ.get("ANTHROPIC_BASE_URL")
    if base:
        print(f"   note ANTHROPIC_BASE_URL={base} is set in your shell")
        print("        the CLI authenticates against that proxy, not your personal key")
    env = dict(os.environ); env["ANTHROPIC_API_KEY"] = key; env.pop("ANTHROPIC_BASE_URL", None)
    try:
        p = subprocess.run(["claude", "-p", "Reply with exactly: ok"],
                           capture_output=True, text=True, timeout=120, env=env)
        out = (p.stdout or "").strip()
        if p.returncode == 0 and "Invalid API key" not in out and out:
            print(f"   OK subprocess -> {out[:60]!r}")
        else:
            print(f"   -- rc={p.returncode} {(out or p.stderr)[:120]!r}")
    except FileNotFoundError:
        print("   -- `claude` not on PATH")
    except subprocess.TimeoutExpired:
        print("   -- timed out")

    print("\nRESULT")
    print("   fit scoring will use:", "api" if api_ok else "cli fallback / unavailable")
    return 0 if api_ok else 1
