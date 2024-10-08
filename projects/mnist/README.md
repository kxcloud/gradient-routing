# MNIST representation splitting

This directory covers gradient routing applied to split representations in an MNIST autoencoder. The implementation for this is in `representation_splitting.py`, particularly via the `SplitAutoencoder` class and the `calculate_split_losses()` function. There is some boilerplate there to enable easy configuration. The ablation results in the paper's appendix were generated by `runs_for_paper_main.py` and tabulated in `analyze_runs_for_paper_main.py`.