#!/bin/zsh
# Daily pipeline refresh. Posting age is the dominant variable, so the point of running
# this every morning is to see a req while it is still days old rather than weeks.
cd "$(dirname "$0")"
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
LOG="daily.log"
{
  echo "===== $(date '+%Y-%m-%d %H:%M') ====="
  ./.venv/bin/python -m careerops.cli sync          2>&1 | grep -v Warning | grep -v warnings.warn
  ./.venv/bin/python -m careerops.cli discover      2>&1 | grep -v Warning | grep -v warnings.warn
  ./.venv/bin/python -m careerops.cli fit --limit 25 2>&1 | grep -v Warning | grep -v warnings.warn
  ./.venv/bin/python -m careerops.cli dashboard     2>&1 | grep -v Warning | grep -v warnings.warn
  echo "--- freshest prospects ---"
  ./.venv/bin/python -m careerops.cli prospects --min 70 2>&1 | grep -v Warning | grep -v warnings.warn | head -14
} >> "$LOG" 2>&1
# keep the log from growing without bound
tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
