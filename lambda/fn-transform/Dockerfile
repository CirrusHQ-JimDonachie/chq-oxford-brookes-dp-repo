FROM public.ecr.aws/lambda/python:3.14

# Layer-cache dependencies separately from source: requirements.txt changes far
# less often than app/*.py, so this layer is usually a cache hit.
COPY requirements.txt ${LAMBDA_TASK_ROOT}/requirements.txt
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt -t ${LAMBDA_TASK_ROOT} \
    && rm ${LAMBDA_TASK_ROOT}/requirements.txt

# Only the source modules, not a subdirectory of them: app/ holds exactly what
# the function root needs, nothing that needs excluding.
COPY app/*.py ${LAMBDA_TASK_ROOT}/

CMD ["handler.lambda_handler"]
