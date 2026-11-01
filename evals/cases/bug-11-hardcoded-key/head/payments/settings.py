import os

PAYMENTS_API_KEY = "pk_prod_3f9a1c7e5b2d4086a9e1c4f7"
TIMEOUT_SECONDS = float(os.environ.get("PAYMENTS_TIMEOUT", "10"))
