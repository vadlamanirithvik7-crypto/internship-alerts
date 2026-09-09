# Ranking evaluation

Run `python evaluation/evaluate.py evaluation/demo_dataset.json --output evaluation/demo_results.json`.

This checked-in dataset is **synthetic**, with 18 fictional jobs, three sample profiles, and 54 explicit graded judgments. It checks that metrics and the product demo work; it is not a real-world accuracy claim. The results include keyword-only, weighted-rule, and semantic-hybrid rankings, precision@10, nDCG@10, first-run latency, and warm-cache latency. Paid API calls are zero. The hybrid method is not uniformly better: in this fixture the embedded profile's nDCG is lower than the weighted baseline. Do not turn these numbers into a resume claim.

For a defensible benchmark, collect real postings using `python evaluation/collect.py`, then have the user label relevance 0–3 for each profile. Keep development and test posting IDs disjoint. Tune weights on development only, freeze the method, and run `--split test` once. The evaluator excludes missing judgments rather than treating them as negative. Labels and real resumes should stay in `evaluation/local/`, which is gitignored.

Input fields are documented in `evaluate.py`. A relevance of 2 or 3 counts as relevant for precision. nDCG uses all four relevance levels. Latency includes inference/cache lookup but not HTTP rendering. Neither similarity nor relevance is a hiring probability.
