name: Pipeline Command — Daily Search

on:
  # Runs at 8:00 AM EST = 13:00 UTC (accounts for EST = UTC-5)
  schedule:
    - cron: "0 13 * * 1-5"   # Weekdays only. Change to "0 13 * * *" for 7 days.

  # Also allows manual trigger from GitHub Actions UI
  workflow_dispatch:
    inputs:
      reason:
        description: "Reason for manual run"
        required: false
        default: "Manual trigger"

jobs:
  search:
    name: Run daily job search
    runs-on: ubuntu-latest
    timeout-minutes: 30

    permissions:
      contents: write   # needed to commit results.json back to repo

    steps:
      # ── 1. Check out repo ──
      - name: Checkout repository
        uses: actions/checkout@v4

      # ── 2. Set up Python ──
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      # ── 3. No extra packages needed ──
      # search_agent.py uses only stdlib (urllib, json, re, os, hashlib)
      # If you add dependencies later, add:
      #   run: pip install <package>

      # ── 4. Run the search agent ──
      - name: Run search agent
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: python search_agent.py

      # ── 5. Commit results.json and history.json back to repo ──
      - name: Commit results
        run: |
          git config user.name  "Pipeline Command Bot"
          git config user.email "pipeline-bot@users.noreply.github.com"
          git add results.json history.json || true
          git diff --staged --quiet || git commit -m "Search results: $(date -u '+%Y-%m-%d %H:%M UTC')"
          git push
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}

      # ── 6. Print summary to Actions log ──
      - name: Summary
        if: always()
        run: |
          echo "## Pipeline Command Search Run" >> $GITHUB_STEP_SUMMARY
          echo "**Completed:** $(date -u '+%Y-%m-%d %H:%M UTC')" >> $GITHUB_STEP_SUMMARY
          if [ -f results.json ]; then
            COUNT=$(python -c "import json; d=json.load(open('results.json')); print(d.get('new_count',0))")
            TOTAL=$(python -c "import json; d=json.load(open('results.json')); print(d.get('total_count',0))")
            echo "**New matches this run:** $COUNT" >> $GITHUB_STEP_SUMMARY
            echo "**Total pending in queue:** $TOTAL" >> $GITHUB_STEP_SUMMARY
          fi
