"""Touch-gesture ML pipeline (exploratory).

Pure-Python, dependency-free at runtime: segmentation, features and the
inert classifier import only the stdlib. scikit-learn / joblib are needed
**only** to train and to run a trained model, and are imported lazily (see
``touch_classifier`` / ``scripts/train_touch_model.py``) so the base app and
tests run without them.

Models are **per skin type** (``skin.skin_type``) with coordinate-free,
index-based features — see ``docs/TOUCH_ML.md`` and the study plan.
"""
