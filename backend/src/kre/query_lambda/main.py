"""Query Lambda handler entry point.

Wraps the FastAPI app with Mangum for AWS Lambda execution.
"""

from mangum import Mangum

from kre.query_lambda.api.main import app

handler = Mangum(app)
