terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

provider "aws" {
  region = var.region
}

data "aws_caller_identity" "current" {}

# No AWS Academy nÃ£o dÃ¡ para criar papÃ©is IAM: usamos o LabRole, que jÃ¡ existe.
data "aws_iam_role" "lab" {
  name = "LabRole"
}

# ---------- Documentos e Ã­ndice ----------
# O bucket é criado pelo aws CLI (passo 4 do README), não pelo Terraform: o provedor
# AWS lê a configuração de Object Lock ao criar o bucket, e a política (SCP) do
# laboratório nega s3:GetBucketObjectLockConfiguration.
data "aws_s3_bucket" "docs" {
  bucket = "${var.name_prefix}-docs-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "docs" {
  bucket                  = data.aws_s3_bucket.docs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---------- Registro de cada consulta ----------
resource "aws_dynamodb_table" "log" {
  name         = "${var.name_prefix}-log"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "request_id"

  attribute {
    name = "request_id"
    type = "S"
  }
}

# ---------- FunÃ§Ãµes ----------
data "archive_file" "code" {
  type        = "zip"
  source_dir  = "${path.module}/lambda"
  output_path = "${path.module}/build/lambda.zip"
}

resource "aws_cloudwatch_log_group" "ingest" {
  name              = "/aws/lambda/${var.name_prefix}-ingest"
  retention_in_days = 7
}

resource "aws_cloudwatch_log_group" "ask" {
  name              = "/aws/lambda/${var.name_prefix}-ask"
  retention_in_days = 7
}

resource "aws_cloudwatch_log_group" "agent" {
  name              = "/aws/lambda/${var.name_prefix}-agent"
  retention_in_days = 7
}

locals {
  common_env = {
    GEMINI_API_KEY   = var.gemini_api_key
    GEMINI_MODEL     = var.gemini_model
    EMBED_MODEL      = var.embed_model
    BUCKET           = data.aws_s3_bucket.docs.id
    TOP_K            = tostring(var.top_k)
    MAX_OUTPUT_TOKEN = tostring(var.max_output_tokens)
    LOG_TABLE        = aws_dynamodb_table.log.name
  }
}

resource "aws_lambda_function" "ingest" {
  function_name    = "${var.name_prefix}-ingest"
  role             = data.aws_iam_role.lab.arn
  runtime          = "python3.12"
  handler          = "ingest.handler"
  filename         = data.archive_file.code.output_path
  source_code_hash = data.archive_file.code.output_base64sha256
  timeout          = 120
  memory_size      = 512

  environment {
    variables = local.common_env
  }

  depends_on = [aws_cloudwatch_log_group.ingest]
}

resource "aws_lambda_function" "ask" {
  function_name    = "${var.name_prefix}-ask"
  role             = data.aws_iam_role.lab.arn
  runtime          = "python3.12"
  handler          = "ask.handler"
  filename         = data.archive_file.code.output_path
  source_code_hash = data.archive_file.code.output_base64sha256
  timeout          = 30 # o API Gateway HTTP corta em 30 s
  memory_size      = 512

  environment {
    variables = local.common_env
  }

  depends_on = [aws_cloudwatch_log_group.ask]
}

# Extensão: o modelo decide quando e quantas vezes buscar (POST /agent).
resource "aws_lambda_function" "agent" {
  function_name    = "${var.name_prefix}-agent"
  role             = data.aws_iam_role.lab.arn
  runtime          = "python3.12"
  handler          = "agent.handler"
  filename         = data.archive_file.code.output_path
  source_code_hash = data.archive_file.code.output_base64sha256
  timeout          = 30 # o API Gateway HTTP corta em 30 s
  memory_size      = 512

  environment {
    variables = merge(local.common_env, {
      AGENT_MAX_STEPS = tostring(var.agent_max_steps)
    })
  }

  depends_on = [aws_cloudwatch_log_group.agent]
}

# ---------- API ----------
resource "aws_apigatewayv2_api" "http" {
  name          = "${var.name_prefix}-api"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "ask" {
  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.ask.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "ask" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "POST /ask"
  target    = "integrations/${aws_apigatewayv2_integration.ask.id}"
}

resource "aws_apigatewayv2_integration" "agent" {
  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.agent.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "agent" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "POST /agent"
  target    = "integrations/${aws_apigatewayv2_integration.agent.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.http.id
  name        = "$default"
  auto_deploy = true

  # Guardrail de custo: limita a taxa de chamadas Ã  API.
  default_route_settings {
    throttling_burst_limit = 5
    throttling_rate_limit  = 2
  }
}

resource "aws_lambda_permission" "api_agent" {
  statement_id  = "AllowApiGatewayAgent"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.agent.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/*"
}

resource "aws_lambda_permission" "api" {
  statement_id  = "AllowApiGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ask.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/*"
}
