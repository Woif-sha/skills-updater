# Update critical-path benchmark (issue 3)

Measured on 2026-08-09 with Python 3.12.10 on Windows 10. The fixture has 160
payload files and approximately 3.2 MB of content, close to the largest installed
Skill observed while preparing the benchmark (169 files, approximately 3.1 MB).
Each result is the median of five clean runs.

Run the benchmark from the repository root:

```powershell
python benchmarks\benchmark_update_paths.py
```

Remote HEAD probes and snapshot downloads are deterministic fakes, and the Git
path uses a local bare repository. The benchmark therefore measures stable local
costs and request counts, not variable Internet latency. Category timings are
inclusive and must not be added together; for example, signature time includes
its directory scans.

## Results

| Path | Median wall time | Remote operations | Signatures | File / directory scans | Full payload copies | Archive extracts | Durable transaction writes |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| snapshot update | 9,295 ms | 2 HEAD probes + 2 downloads | 15 | 23 / 24 | 5 | 2 | 14 |
| Git worktree update | 15,078 ms | 2 fetches | 9 | 11 / 11 | 2 | 1 | 12 |
| metadata-only | 185 ms | 1 HEAD probe | 0 | 0 / 0 | 0 | 0 | 11 |

The snapshot path spent 2,859 ms in signatures and 2,207 ms in copy calls. The
file scans nested inside those operations accumulated 2,460 ms. Archive extraction
was 313 ms. The Git path issued 82 subprocess calls (4,653 ms accumulated), of
which two were fetches, and spent 1,734 ms in signatures. Metadata-only spent
48 ms in its 11 transaction writes; its two registry synchronizations and three
registry writes took about 58 ms together.

## Priority order

1. **Remove the second remote probe/fetch.** Carry the exact remote revision from
   the CLI probe into resolution/apply. Under the Skill lock, revalidate local-only,
   metadata, branch, origin, HEAD, and the already-fetched remote ref without
   fetching again. This changes snapshot HEAD probes from 2 to 1 and Git fetches
   from 2 to 1 without weakening the compare-and-swap checks.
2. **Make prepared payload evidence a transaction-owned value.** Carry a staged
   payload's path and signature together instead of discarding the signature and
   rescanning an immutable staging directory. Reuse the verified original snapshot
   as the successful backup record instead of making and signing another full copy.
   Keep verification at mutation and publication seams; remove only repeated work
   on transaction-owned immutable data.
3. **Collapse the final registry reconciliation and status overlay into one locked
   write.** Every path currently performs two full registry synchronizations and
   three registry writes. The synthetic fixture contains one entry, so this result
   understates the cost for a real root with many installed Skills.
4. **Keep archive caching and phase-write reduction out of the first pass.** The
   remote and base archives are different revisions, so the measured two extracts
   do not demonstrate reusable work. Transaction writes cost tens of milliseconds
   and encode crash-recovery state; deleting them needs a separate correctness
   proof and offers less value than removing duplicate network and payload work.

The first two changes should meet at a transaction seam whose interface accepts
an exact remote revision and prepared, verified payload evidence. That keeps the
optimization local to one deep module instead of spreading caches or optional
fast paths across the CLI, snapshot updater, and Git updater.
