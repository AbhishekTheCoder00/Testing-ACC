"""
Purpose: Databricks clients used only by the headless M2M path. Kept separate from
clients/databricks_client.py, which is the workspace REST surface shared by both paths —
this package is about obtaining a service-principal credential, not about calling the API.
"""
