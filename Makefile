# Lambda deployment-package build (PLAN.md Phase 17).
#
# Produces a zip deployment package with Linux wheels cross-compiled on any
# host — no Docker, no container image, no ECR. `cryptography`'s manylinux wheel
# carries its compiled binary, so `--only-binary=:all:` + a manylinux platform
# is all it takes. boto3 is provided by the Lambda runtime and is deliberately
# NOT installed here, keeping the runtime dependency cap intact.
#
# Measured target: ~5.5 MB zipped / ~18 MB unzipped, inside Lambda's 50/250 MB
# limits. `make lambda-zip` prints the actual sizes so a wild deviation is
# visible before deploy.

PYTHON       ?= python3
BUILD_DIR    := build/lambda
ZIP          := build/monitor-lambda.zip
PLATFORM     := manylinux2014_x86_64
PY_VERSION   := 3.12

.PHONY: lambda-zip lambda-clean

lambda-zip: lambda-clean
	mkdir -p $(BUILD_DIR)
	# Runtime deps only (httpx[http2], PyJWT, cryptography). --platform +
	# --only-binary pull the Linux wheels regardless of the build host's OS.
	$(PYTHON) -m pip install \
		-r requirements.txt \
		--target $(BUILD_DIR) \
		--platform $(PLATFORM) \
		--python-version $(PY_VERSION) \
		--implementation cp \
		--only-binary=:all: \
		--upgrade
	# Flatten the monitor onto the zip root so `import monitor`, `import apns`,
	# `from providers import ...` resolve the same way pytest imports them.
	cp lambda_function.py $(BUILD_DIR)/
	cp scripts/*.py $(BUILD_DIR)/
	cp -R scripts/providers $(BUILD_DIR)/providers
	# Drop caches and bytecode; boto3/botocore must never be here.
	find $(BUILD_DIR) -type d -name __pycache__ -prune -exec rm -rf {} +
	find $(BUILD_DIR) -type d \( -name 'boto3*' -o -name 'botocore*' \) -prune -exec rm -rf {} +
	cd $(BUILD_DIR) && zip -q -r -X ../monitor-lambda.zip . -x '*.pyc'
	@echo "----"
	@echo "zipped:   $$(du -h $(ZIP) | cut -f1)  ($(ZIP))"
	@echo "unzipped: $$(du -sh $(BUILD_DIR) | cut -f1)"

lambda-clean:
	rm -rf $(BUILD_DIR) $(ZIP)
