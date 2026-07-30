FROM public.ecr.aws/lambda/python:3.12

# Install Java 17 (Amazon Linux 2023 uses dnf)
RUN dnf install -y java-17-amazon-corretto-headless && dnf clean all

# Copy requirements
COPY requirements.txt ${LAMBDA_TASK_ROOT}/

# Install python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy function code
COPY main.py ${LAMBDA_TASK_ROOT}/

# Set the CMD to your handler
CMD ["main.lambda_handler"]
