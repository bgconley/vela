# GitHub/default-branch state

Live check at `2026-07-13T08:22:20Z`:

```text
refs/heads/main                                88d18d897aabe87184a10575bcf8b52842ff20af
refs/heads/remediate/2026-07-09-review         de9b0a13f2ef7166014794bb211845dd3ba96123
```

- The remediation branch's workflow is dispatch-only.
- Scheduled run [29188445178](https://github.com/bgconley/vela/actions/runs/29188445178)
  is completed with conclusion `cancelled`.
- The live workflow file on `main` still contains `schedule: - cron: "17 8 * * *"` because `main`
  has not yet received the remediation branch.
- Therefore default-branch dispatch-only activation is merge-dependent and is not claimed as
  complete in this evidence set.
- The README's unqualified Git install and clone URLs also resolve `main`; literal default-branch
  quickstart certification is merge-dependent. Exact-SHA supplemental lanes are green under
  `quickstarts/`.
