"""Connections to the services a person links with their own key: SnapTrade (brokerages) and SimpleFIN (banks).

Read only. The fetchers return models.py dataclasses; the service layer writes them through the store. Nothing
in this package is imported by this __init__, because store.py imports names.py.
"""
