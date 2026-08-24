FROM public.ecr.aws/lambda/python:3.12

# Install Java Runtime Environment for opendataloader-pdf
RUN dnf install -y java-17-amazon-corretto-headless && dnf clean all

# Install Python dependencies
COPY requirements.txt ${LAMBDA_TASK_ROOT}
RUN pip install -r requirements.txt

# Copy handler code
COPY main.py ${LAMBDA_TASK_ROOT}

# Set the CMD to your handler
CMD [ "main.handler" ]
