from constructs import Construct
import os
import re
import aws_cdk.aws_s3 as s3
import aws_cdk.aws_stepfunctions as sfn
import aws_cdk.aws_stepfunctions_tasks as tasks
import aws_cdk.aws_lambda as lambda_
import aws_cdk.aws_lambda_event_sources as eventsources
import aws_cdk.aws_iam as iam
import amazon_textract_idp_cdk_constructs as tcdk
from aws_cdk import CfnOutput, RemovalPolicy, Stack, Duration, Aws, Fn
from aws_solutions_constructs.aws_lambda_opensearch import LambdaToOpenSearch
from aws_cdk import aws_opensearchservice as opensearch


class OpenSearchWorkflow(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(
            scope,
            construct_id,
            description="IDP CDK constructs sample for ingesting large number of documents to OpenSearch (SO9217)",
            **kwargs,
        )

        script_location = os.path.dirname(__file__)
        s3_upload_prefix = "uploads"
        s3_output_prefix = "textract-output"
        s3_opensearch_output_prefix = "textract-opensearch-output"
        s3_temp_output_prefix = "textract-temp"

        #######################################
        # BEWARE! This is a demo/POC setup
        # Remove the auto_delete_objects=True and removal_policy=RemovalPolicy.DESTROY
        # when the documents should remain after deleting the CloudFormation stack!
        #######################################

        # Create the bucket for the documents and outputs
        document_bucket = s3.Bucket(
            self,
            "OpenSearchWorkflowBucket",
            auto_delete_objects=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        s3_output_bucket = document_bucket.bucket_name
        # get the event source that will be used later to trigger the executions
        s3_event_source = eventsources.S3EventSource(
            document_bucket,
            events=[s3.EventType.OBJECT_CREATED],
            filters=[s3.NotificationKeyFilter(prefix=s3_upload_prefix)],
        )

        workflow_name = "OpenSearchWorkflow"
        current_region = Stack.of(self).region
        account_id = Stack.of(self).account
        stack_name = Stack.of(self).stack_name

        # the decider checks if the document is of valid format and gets the
        # number of pages
        decider_task = tcdk.TextractPOCDecider(
            self,
            f"{workflow_name}-Decider",
            textract_decider_max_retries=10000,
        )

        # The splitter takes a document and splits into the max_number_of_pages_per_document
        # This is particulary useful when working with documents that exceed the Textract limits
        # or when the workflow requires per page processing
        document_splitter_task = tcdk.DocumentSplitter(
            self,
            "DocumentSplitter",
            s3_output_bucket=s3_output_bucket,
            s3_output_prefix=s3_output_prefix,
            max_number_of_pages_per_doc=2500,
            lambda_log_level="INFO",
            textract_document_splitter_max_retries=10000,
        )

        # Calling Textract asynchronous
        textract_async_task = tcdk.TextractGenericAsyncSfnTask(
            self,
            "TextractAsync",
            s3_output_bucket=s3_output_bucket,
            s3_temp_output_prefix=s3_temp_output_prefix,
            textract_async_call_max_retries=50000,
            integration_pattern=sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
            lambda_log_level="INFO",
            timeout=Duration.hours(24),
            input=sfn.TaskInput.from_object(
                {
                    "Token": sfn.JsonPath.task_token,
                    "ExecutionId": sfn.JsonPath.string_at("$$.Execution.Id"),
                    "Payload": sfn.JsonPath.entire_payload,
                }
            ),
            result_path="$.textract_result",
        )

        # Converting the potentially paginated output from Textract to a single JSON file
        textract_async_to_json = tcdk.TextractAsyncToJSON(
            self,
            "TextractAsyncToJSON2",
            lambda_log_level="INFO",
            s3_output_prefix=s3_output_prefix,
            s3_output_bucket=s3_output_bucket,
        )

        # For the import into OpenSearch, best practice is to use the bulk format
        # This Lambda creates a bulk for each split from the DocumentSplitter
        generate_open_search_batch = tcdk.TextractGenerateCSV(
            self,
            "GenerateOpenSearchBatch",
            csv_s3_output_bucket=document_bucket.bucket_name,
            csv_s3_output_prefix=s3_opensearch_output_prefix,
            lambda_log_level="INFO",
            lambda_memory_mb=10240,
            output_type="OPENSEARCH_BATCH",
            opensearch_index_name="papers-index",
            integration_pattern=sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
            input=sfn.TaskInput.from_object(
                {
                    "Token": sfn.JsonPath.task_token,
                    "ExecutionId": sfn.JsonPath.string_at("$$.Execution.Id"),
                    "Payload": sfn.JsonPath.entire_payload,
                }
            ),
            result_path="$.opensearch_output_location",
        )

        # Pushing the OpenSearch bulk import file into OpenSearch
        lambda_opensearch_push = lambda_.DockerImageFunction(  # type: ignore
            self,
            "LambdaOpenSearchPush",
            code=lambda_.DockerImageCode.from_image_asset(
                os.path.join(script_location, "../lambda/opensearch_push/")
            ),
            memory_size=10240,
            timeout=Duration.seconds(900),
            architecture=lambda_.Architecture.X86_64,
            environment={"LOG_LEVEL": "INFO"},
        )

        lambda_opensearch_push.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:Get*", "s3:List*"],
                resources=[
                    document_bucket.bucket_arn,
                    document_bucket.bucket_arn + "/*",
                ],
            )
        )

        task_lambda_opensearch_push = tasks.LambdaInvoke(
            self,
            "OpenSearchPushInvoke",
            lambda_function=lambda_opensearch_push,  # type: ignore
            timeout=Duration.seconds(900),
            payload_response_only=True,
            result_path="$.OpenSearchPush",
        )

        task_lambda_opensearch_push.add_retry(
            max_attempts=100000,
            errors=["Lambda.TooManyRequestsException", "OpenSearchConnectionTimeout"],
        )

        # Creating the OpenSearch instances and connect with the OpenSearch push Lambda
        cognito_stack_name = re.sub("[^a-zA-Z0-9-]", "", f"{stack_name}").lower()[:30]
        lambda_to_opensearch = LambdaToOpenSearch(
            self,
            "OpenSearchResources",
            existing_lambda_obj=lambda_opensearch_push,
            open_search_domain_name="idp-cdk-opensearch",
            cognito_domain_name=f"{cognito_stack_name}-{account_id}-{current_region}",
            open_search_domain_props=opensearch.CfnDomainProps(
                ebs_options=opensearch.CfnDomain.EBSOptionsProperty(
                    volume_size=200, volume_type="gp2"
                ),
                cluster_config=opensearch.CfnDomain.ClusterConfigProperty(
                    instance_type="m6g.large.search"
                ),
                engine_version="OpenSearch_2.7",
            ),
        )

        # The mapping function is just to generate an empty output from the Map state
        lambda_opensearch_mapping: lambda_.IFunction = lambda_.DockerImageFunction(  # type: ignore
            self,
            "LambdaOpenSearchMapping",
            code=lambda_.DockerImageCode.from_image_asset(
                os.path.join(script_location, "../lambda/map_opensearch_lambda/")
            ),
            memory_size=128,
            architecture=lambda_.Architecture.X86_64,
            environment={},
        )

        task_opensearch_mapping = tasks.LambdaInvoke(
            self,
            "TaskOpenSearchMapping",
            lambda_function=lambda_opensearch_mapping,
            output_path="$.Payload",
        )

        task_opensearch_mapping.add_retry(
            max_attempts=100000,
            errors=["Lambda.TooManyRequestsException"],
        )
        # Setting meta-data for the SearchIndex
        set_meta_data_function: lambda_.IFunction = lambda_.DockerImageFunction(  # type: ignore
            self,
            "SetMetaDataFunction",
            code=lambda_.DockerImageCode.from_image_asset(
                os.path.join(
                    script_location, "../lambda/set-manifest-meta-data-opensearch"
                )
            ),
            memory_size=128,
            architecture=lambda_.Architecture.X86_64,
            environment={"LOG_LEVEL": "ERROR"},
        )

        # Setting meta data like the origin filename and the page number for the ingest to OpenSearch
        set_meta_data_task = tasks.LambdaInvoke(
            self,
            "SetMetaData",
            lambda_function=set_meta_data_function,
            output_path="$.Payload",
        )

        set_meta_data_task.add_retry(
            max_attempts=10000,
            errors=["Lambda.TooManyRequestsException"],
        )

        ## Creating the StepFunction workflow
        async_chain = sfn.Chain.start(textract_async_task).next(textract_async_to_json)

        textract_async_to_json.next(set_meta_data_task).next(generate_open_search_batch)

        generate_open_search_batch.next(task_lambda_opensearch_push).next(
            task_opensearch_mapping
        )

        map = sfn.Map(
            self,
            "Map State",
            items_path=sfn.JsonPath.string_at("$.pages"),
            parameters={
                "manifest": {
                    "s3Path": sfn.JsonPath.string_at(
                        "States.Format('s3://{}/{}/{}', \
                  $.documentSplitterS3OutputBucket, \
                  $.documentSplitterS3OutputPath, \
                  $$.Map.Item.Value)"
                    )
                },
                "mime": sfn.JsonPath.string_at("$.mime"),
                "originFileURI": sfn.JsonPath.string_at("$.originFileURI"),
            },
        )

        map.iterator(async_chain)

        workflow_chain = (
            sfn.Chain.start(decider_task).next(document_splitter_task).next(map)
        )

        # GENERIC
        state_machine = sfn.StateMachine(self, workflow_name, definition=workflow_chain)

        # The StartThrottle triggers based on event_source (in this case S3 OBJECT_CREATED)
        # and handles all the complexity of making sure the limits or bottlenecks are not exceeded
        sf_executions_start_throttle = tcdk.SFExecutionsStartThrottle(
            self,
            "ExecutionThrottle",
            state_machine_arn=state_machine.state_machine_arn,
            executions_concurrency_threshold=550,
            sqs_batch=10,
            lambda_log_level="ERROR",
            event_source=[s3_event_source],
        )

        queue_url_urlencoded = ""
        if sf_executions_start_throttle.document_queue:
            # urlencode the SQS Queue link, otherwise the deep linking does not work properly.
            queue_url_urlencoded = Fn.join(
                "%2F",
                Fn.split(
                    "/",
                    Fn.join(
                        "%3A",
                        Fn.split(
                            ":", sf_executions_start_throttle.document_queue.queue_url
                        ),
                    ),
                ),
            )
        # OUTPUT
        CfnOutput(
            self,
            "DocumentUploadLocation",
            value=f"s3://{document_bucket.bucket_name}/{s3_upload_prefix}/",
            export_name=f"{Aws.STACK_NAME}-DocumentUploadLocation",
        )
        CfnOutput(
            self,
            "StepFunctionFlowLink",
            value=f"https://{current_region}.console.aws.amazon.com/states/home?region={current_region}#/statemachines/view/{state_machine.state_machine_arn}",
        ),
        CfnOutput(
            self,
            "DocumentQueueLink",
            value=f"https://{current_region}.console.aws.amazon.com/sqs/v2/home?region={current_region}#/queues/{queue_url_urlencoded}",  # type: ignore
        )
        CfnOutput(
            self,
            "OpenSearchDashboard",
            value=f"https://{lambda_to_opensearch.open_search_domain.attr_domain_endpoint}/_dashboards",
        )
        CfnOutput(
            self,
            "OpenSearchLink",
            value=f"https://{current_region}.console.aws.amazon.com/aos/home?region={current_region}#/opensearch/domains/{lambda_to_opensearch.open_search_domain.domain_name}",
        )
        # Link to UserPool
        CfnOutput(
            self,
            "CognitoUserPoolLink",
            value=f"https://{current_region}.console.aws.amazon.com/cognito/v2/idp/user-pools/{lambda_to_opensearch.user_pool.user_pool_id}/users?region={current_region}",
        )
