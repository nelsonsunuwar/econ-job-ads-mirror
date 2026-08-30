# econ-job-ads-mirror

Daily slim index of publicly listed economics job ads, for a personal job-search
digest. A GitHub Action runs `fetch.py` every morning and commits `ads.json`.

Only listing **metadata** is kept — institution, title, location, field tags,
deadline, and a link to the original ad. No ad texts are reproduced; follow the
`url` of each entry to the original posting.

Sources: [EconJobMarket](https://econjobmarket.org) (public JSON API),
[AEA JOE](https://www.aeaweb.org/joe/listings) (official XML download),
[jobs.ac.uk](https://www.jobs.ac.uk/categories/economics),
[NABE econjobs](https://econjobs.nabe.com/jobs/),
[econ-jobs.com](https://econ-jobs.com) (sitemap).

`ads.json` shape: `{generated_at, sources: {name: {status, count}}, ads: [...]}`.
