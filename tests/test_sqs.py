# Copyright The Cloud Custodian Authors.
# SPDX-License-Identifier: Apache-2.0
from .common import BaseTest, functional, event_data
from datetime import datetime
from dateutil.tz import tzutc
from pytest_terraform import terraform
from botocore.exceptions import ClientError


import json
import logging
import pytest
import time

from c7n.resources.aws import shape_validate, Arn


def test_sqs_config_translate(test):
    # we're using a cwe event as a config, so have to mangle to
    # config's inane format (json strings in json)
    event = event_data('sqs-discover.json')
    p = test.load_policy({
        'name': 'sqs-check',
        'resource': 'aws.sqs',
        'mode': {'type': 'config-rule'}})
    config = p.resource_manager.get_source('config')
    resource = config.load_resource(event['detail']['configurationItem'])
    Arn.parse(resource['QueueArn']).resource == 'config-changes'
    assert resource == {
        'CreatedTimestamp': datetime(
            2020, 10, 6, 22, 27, 29, tzinfo=tzutc()),
        'DelaySeconds': '0',
        'LastModifiedTimestamp': datetime(
            2020, 10, 6, 22, 27, 29, tzinfo=tzutc()),
        'MaximumMessageSize': '262144',
        'MessageRetentionPeriod': '345600',
        'Policy': '{"Version":"2012-10-17","Statement":[{"Sid":"","Effect":"Allow","Principal":{"Service":"events.amazonaws.com"},"Action":"sqs:SendMessage","Resource":"arn:aws:sqs:us-east-1:644160558196:config-changes"}]}', # noqa
        'QueueArn': 'arn:aws:sqs:us-east-1:644160558196:config-changes',
        'QueueUrl': 'https://sqs.us-east-1.amazonaws.com/644160558196/config-changes',
        'ReceiveMessageWaitTimeSeconds': '0',
        'VisibilityTimeout': '30',
    }


@terraform('sqs_delete', teardown=terraform.TEARDOWN_IGNORE)
def test_sqs_delete(test, sqs_delete):
    session_factory = test.replay_flight_data("test_sqs_delete", region='us-east-2')
    client = session_factory().client("sqs")
    queue_arn = sqs_delete["aws_sqs_queue.test_sqs.arn"]

    p = test.load_policy(
        {
            "name": "sqs-delete",
            "resource": "sqs",
            "filters": [{"QueueArn": queue_arn}],
            "actions": [{"type": "delete"}],
        },
        config={'region': 'us-east-2'},
        session_factory=session_factory,
    )

    if test.recording:
        time.sleep(60)

    resources = p.run()
    test.assertEqual(len(resources), 1)

    queue_url = resources[0]['QueueUrl']
    pytest.raises(ClientError, client.purge_queue, QueueUrl=queue_url)

    if test.recording:
        time.sleep(2)


@terraform('sqs_set_encryption')
def test_sqs_set_encryption(test, sqs_set_encryption):
    session_factory = test.replay_flight_data("test_sqs_set_encryption", region='us-west-2')

    key_id = sqs_set_encryption["aws_kms_key.test_key.key_id"]
    queue_arn = sqs_set_encryption["aws_sqs_queue.test_sqs.arn"]
    alias_name = sqs_set_encryption["aws_kms_alias.test_key_alias.name"]

    client = session_factory().client("sqs")

    if test.recording:
        time.sleep(30)

    p = test.load_policy(
        {
            "name": "sqs-set-encryption",
            "resource": "sqs",
            "filters": [{"QueueArn": queue_arn}],
            "actions": [{"type": "set-encryption", "key": alias_name.replace('alias/', '')}],
        },
        config={'region': 'us-west-2'},
        session_factory=session_factory,
    )
    resources = p.run()

    queue_url = resources[0]["QueueUrl"]

    queue_attributes = client.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["All"])
    check_master_key = queue_attributes["Attributes"]["KmsMasterKeyId"]
    test.assertEqual(check_master_key, key_id)


@terraform('sqs_remove_matched')
def test_sqs_remove_matched(test, sqs_remove_matched):
    session_factory = test.replay_flight_data("test_sqs_remove_matched", region="us-east-2")
    queue_arn = sqs_remove_matched['aws_sqs_queue.test_sqs.arn']
    client = session_factory().client("sqs")

    if test.recording:
        time.sleep(60)

    p = test.load_policy(
        {
            "name": "sqs-rm-matched",
            "resource": "sqs",
            "filters": [
                {"QueueArn": queue_arn},
                {"type": "cross-account", "everyone_only": True},
            ],
            "actions": [{"type": "remove-statements", "statement_ids": "matched"}],
        },
        config={'region': 'us-east-2'},
        session_factory=session_factory,
    )
    resources = p.run()

    queue_url = resources[0]["QueueUrl"]
    queue_attributes = client.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=["Policy"]
    )
    data = json.loads(queue_attributes["Attributes"]["Policy"])

    test.assertEqual([s["Sid"] for s in data.get("Statement", ())], ["SpecificAllow"])


class QueueTests(BaseTest):

    @functional
    def test_sqs_remove_named(self):
        session_factory = self.replay_flight_data("test_sqs_remove_named")
        client = session_factory().client("sqs")
        name = "test-sqs-remove-named"
        queue_url = client.create_queue(QueueName=name)["QueueUrl"]

        def cleanup():
            client.delete_queue(QueueUrl=queue_url)
            if self.recording:
                time.sleep(60)

        self.addCleanup(cleanup)

        client.set_queue_attributes(
            QueueUrl=queue_url,
            Attributes={
                "Policy": json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Sid": "SpecificAllow",
                                "Effect": "Allow",
                                "Principal": {"AWS": "arn:aws:iam::644160558196:root"},
                                "Action": ["sqs:Subscribe"],
                            },
                            {
                                "Sid": "RemoveMe",
                                "Effect": "Allow",
                                "Principal": "*",
                                "Action": ["sqs:GetqueueAttributes"],
                            },
                        ],
                    }
                )
            },
        )
        if self.recording:
            time.sleep(30)

        p = self.load_policy(
            {
                "name": "sqs-rm-named",
                "resource": "sqs",
                "filters": [{"QueueUrl": queue_url}],
                "actions": [
                    {"type": "remove-statements", "statement_ids": ["RemoveMe"]}
                ],
            },
            session_factory=session_factory,
        )

        resources = p.run()
        if self.recording:
            time.sleep(30)
        self.assertEqual(len(resources), 1)

        data = json.loads(
            client.get_queue_attributes(
                QueueUrl=resources[0]["QueueUrl"], AttributeNames=["Policy"]
            )[
                "Attributes"
            ][
                "Policy"
            ]
        )
        self.assertTrue("RemoveMe" not in [s["Sid"] for s in data.get("Statement", ())])

    def test_sqs_remove_all(self):
        factory = self.replay_flight_data("test_sqs_remove_named_all")
        queue_url = "https://queue.amazonaws.com/644160558196/test-sqs-remove-named"
        p = self.load_policy(
            {
                "name": "sqs-rm-all",
                "resource": "sqs",
                "filters": [{"QueueUrl": queue_url}],
                "actions": [
                    {"type": "remove-statements", "statement_ids": ["RemoveMe"]}
                ],
            },
            session_factory=factory,
        )
        resources = p.run()
        self.assertEqual(len(resources), 1)
        client = factory().client("sqs")
        d2 = client.get_queue_attributes(
            QueueUrl=queue_url, AttributeNames=["All"])["Attributes"]
        self.assertNotIn("Policy", d2)

    @functional
    def test_sqs_modify_policy_add_statements(self):
        session_factory = self.replay_flight_data("test_sqs_modify_policy_add_statements")
        client = session_factory().client("sqs")
        name = "test_sqs_modify_policy_add_statements"
        queue_url = client.create_queue(QueueName=name)["QueueUrl"]

        def cleanup():
            client.delete_queue(QueueUrl=queue_url)
            if self.recording:
                time.sleep(60)

        self.addCleanup(cleanup)

        client.set_queue_attributes(
            QueueUrl=queue_url,
            Attributes={
                "Policy": json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Sid": "SpecificAllow",
                                "Effect": "Allow",
                                "Principal": {"AWS": "arn:aws:iam::644160558196:root"},
                                "Action": ["sqs:Subscribe"],
                            },
                        ],
                    }
                ),
            },
        )

        if self.recording:
            time.sleep(30)

        p = self.load_policy(
            {
                "name": "sqs-set-permissions-add-statements-policy",
                "resource": "sqs",
                "filters": [{"QueueUrl": queue_url}],
                "actions": [
                    {
                        "type": "modify-policy",
                        "add-statements": [
                            {
                                "Sid": "AddMe",
                                "Effect": "Allow",
                                "Principal": "*",
                                "Action": ["sqs:GetQueueAttributes"],
                                "Resource": queue_url
                            }
                        ],
                        "remove-statements": [],
                    }
                ],
            },
            session_factory=session_factory,
        )

        resources = p.run()
        if self.recording:
            time.sleep(30)

        self.assertEqual(len(resources), 1)

        data = json.loads(
            client.get_queue_attributes(
                QueueUrl=resources[0]["QueueUrl"], AttributeNames=["Policy"]
            )["Attributes"]["Policy"]
        )

        self.assertTrue("AddMe" in [s["Sid"] for s in data.get("Statement", ())])

    @functional
    def test_sqs_modify_policy_add_remove_statements(self):
        session_factory = self.replay_flight_data("test_sqs_modify_policy_add_remove_statements")
        client = session_factory().client("sqs")
        name = "test_sqs_modify_policy_add_remove_statements"
        queue_url = client.create_queue(QueueName=name)["QueueUrl"]

        def cleanup():
            client.delete_queue(QueueUrl=queue_url)
            if self.recording:
                time.sleep(60)

        self.addCleanup(cleanup)

        client.set_queue_attributes(
            QueueUrl=queue_url,
            Attributes={
                "Policy": json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Sid": "SpecificAllow",
                                "Effect": "Allow",
                                "Principal": {"AWS": "arn:aws:iam::123456789123:root"},
                                "Action": ["sqs:Subscribe"],
                            },
                            {
                                "Sid": "RemoveMe",
                                "Effect": "Allow",
                                "Principal": "*",
                                "Action": ["sqs:GetQueueAttributes"],
                            }
                        ],
                    }
                ),
            },
        )

        if self.recording:
            time.sleep(30)

        p = self.load_policy(
            {
                "name": "sqs_modify_policy_add_remove_statements",
                "resource": "sqs",
                "filters": [{"QueueUrl": queue_url}],
                "actions": [
                    {
                        "type": "modify-policy",
                        "add-statements": [
                            {
                                "Sid": "AddMe",
                                "Effect": "Allow",
                                "Principal": "*",
                                "Action": ["sqs:GetQueueAttributes"],
                                "Resource": queue_url
                            }
                        ],
                        "remove-statements": ["RemoveMe"],
                    }
                ],
            },
            session_factory=session_factory,
        )

        resources = p.run()
        if self.recording:
            time.sleep(30)

        self.assertEqual(len(resources), 1)

        data = json.loads(
            client.get_queue_attributes(
                QueueUrl=resources[0]["QueueUrl"], AttributeNames=["Policy"]
            )["Attributes"]["Policy"]
        )

        statement_ids = {s["Sid"] for s in data.get("Statement", ())}
        self.assertTrue("AddMe" in statement_ids)
        self.assertTrue("RemoveMe" not in statement_ids)
        self.assertTrue("SpecificAllow" in statement_ids)

    @functional
    def test_sqs_mark_for_op(self):
        session_factory = self.replay_flight_data("test_sqs_mark_for_op")
        client = session_factory().client("sqs")
        name = "test-sqs"
        queue_url = client.create_queue(QueueName=name)["QueueUrl"]
        self.addCleanup(client.delete_queue, QueueUrl=queue_url)

        if self.recording:
            time.sleep(15)

        p = self.load_policy(
            {
                "name": "sqs-mark-for-op",
                "resource": "sqs",
                "filters": [{"QueueUrl": queue_url}],
                "actions": [
                    {
                        "type": "mark-for-op",
                        "tag": "tag-for-op",
                        "op": "delete",
                        "days": 1,
                    }
                ],
            },
            session_factory=session_factory,
        )

        resources = p.run()
        self.assertEqual(len(resources), 1)
        tags_after_run = client.list_queue_tags(QueueUrl=queue_url).get("Tags", {})
        self.assertTrue("tag-for-op" in tags_after_run)

    @functional
    def test_sqs_tag(self):
        session_factory = self.replay_flight_data("test_sqs_tags")
        client = session_factory().client("sqs")
        name = "test-sqs-5"
        queue_url = client.create_queue(QueueName=name)["QueueUrl"]
        self.addCleanup(client.delete_queue, QueueUrl=queue_url)

        if self.recording:
            time.sleep(15)

        p = self.load_policy(
            {
                "name": "sqs-mark-for-op",
                "resource": "sqs",
                "filters": [{"QueueUrl": queue_url}],
                "actions": [
                    {
                        "type": "tag",
                        "key": "tag-this-queue",
                        "value": "This queue has been tagged",
                    }
                ],
            },
            session_factory=session_factory,
        )

        resources = p.run()
        self.assertEqual(len(resources), 1)
        tags_after_run = client.list_queue_tags(QueueUrl=queue_url).get("Tags", {})
        self.assertTrue("tag-this-queue" in tags_after_run)

    @functional
    def test_sqs_remove_tag(self):
        session_factory = self.replay_flight_data("test_sqs_remove_tag")
        client = session_factory().client("sqs")
        name = "test-sqs-4"
        queue_url = client.create_queue(QueueName=name)["QueueUrl"]
        client.tag_queue(
            QueueUrl=queue_url, Tags={"remove-this-tag": "tag to be removed"}
        )
        self.addCleanup(client.delete_queue, QueueUrl=queue_url)

        if self.recording:
            time.sleep(15)

        p = self.load_policy(
            {
                "name": "sqs-mark-for-op",
                "resource": "sqs",
                "filters": [
                    {"QueueUrl": queue_url}, {"tag:remove-this-tag": "present"}
                ],
                "actions": [{"type": "remove-tag", "tags": ["remove-this-tag"]}],
            },
            session_factory=session_factory,
        )

        resources = p.run()
        self.assertEqual(len(resources), 1)
        tags_after_run = client.list_queue_tags(QueueUrl=queue_url).get("Tags", {})
        self.assertTrue("remove-this-tag" not in tags_after_run)

    @functional
    def test_sqs_marked_for_op(self):
        session_factory = self.replay_flight_data("test_sqs_marked_for_op")
        client = session_factory().client("sqs")
        name = "test-sqs"
        queue_url = client.create_queue(QueueName=name)["QueueUrl"]
        client.tag_queue(
            QueueUrl=queue_url,
            Tags={"tag-for-op": "Resource does not meet policy: delete@2017/11/01"},
        )
        self.addCleanup(client.delete_queue, QueueUrl=queue_url)

        if self.recording:
            time.sleep(30)

        p = self.load_policy(
            {
                "name": "sqs-marked-for-op",
                "resource": "sqs",
                "filters": [
                    {"type": "marked-for-op", "tag": "tag-for-op", "op": "delete"}
                ],
            },
            session_factory=session_factory,
        )

        resources = p.run()
        self.assertEqual(len(resources), 1)

    def test_sqs_set_retention(self):
        session = self.replay_flight_data("test_sqs_set_retention")
        client = session(region="us-east-1").client("sqs")
        p = self.load_policy(
            {
                "name": "sqs-reduce-long-retentions",
                "resource": "sqs",
                "filters": [
                    {
                        "type": "value",
                        "value_type": "integer",
                        "key": "MessageRetentionPeriod",
                        "value": 345600,
                        "op": "ge",
                    }
                ],
                "actions": [{"type": "set-retention-period", "period": 86400}],
            },
            session_factory=session,
        )
        resources = p.run()
        self.assertEqual(len(resources), 1)

        retention = client.get_queue_attributes(
            QueueUrl=resources[0]["QueueUrl"], AttributeNames=["MessageRetentionPeriod"]
        )[
            "Attributes"
        ]
        self.assertEqual(int(retention["MessageRetentionPeriod"]), 86400)

    def test_sqs_get_resources(self):
        factory = self.replay_flight_data("test_sqs_get_resources")
        p = self.load_policy(
            {"name": "sqs-reduce", "resource": "sqs"}, session_factory=factory
        )
        url1 = "https://us-east-2.queue.amazonaws.com/644160558196/BrickHouse"
        url2 = "https://sqs.us-east-2.amazonaws.com/644160558196/BrickHouse"
        *_, enum_extra_args = p.resource_manager.resource_type.enum_spec
        # MaxResults arg is required to enable list_queues pagination
        self.assertIn("MaxResults", enum_extra_args)
        resources = p.resource_manager.get_resources([url1])
        self.assertEqual(resources[0]["QueueUrl"], url1)
        resources = p.resource_manager.get_resources([url2])
        self.assertEqual(resources[0]["QueueUrl"], url1)

    def test_sqs_access_denied(self):
        session_factory = self.replay_flight_data("test_sqs_access_denied")
        p = self.load_policy(
            {
                "name": "sqs-list",
                "resource": "sqs",
            },
            session_factory=session_factory
        )
        log_output = self.capture_logging("custodian.resources.sqs", level=logging.WARNING)

        resources = p.run()
        assert len(resources) == 0
        assert "Denied access to sqs" in log_output.getvalue()

    @functional
    def test_sqs_kms_alias(self):
        session_factory = self.replay_flight_data("test_sqs_kms_key_filter")

        p = self.load_policy(
            {
                "name": "sqs-kms-alias",
                "resource": "sqs",
                "filters": [
                    {
                        "or": [
                            {
                                "type": "value",
                                "key": "KmsMasterKeyId",
                                "value": "^(alias/aws/)",
                                "op": "regex"
                            },
                            {
                                "type": "kms-key",
                                "key": "c7n:AliasName",
                                "value": "^(alias/aws/)",
                                "op": "regex"
                            }
                        ]
                    }
                ]
            },
            session_factory=session_factory,
        )
        resources = p.run()
        self.assertEqual(2, len(resources))
        for r in resources:
            self.assertTrue(r['KmsMasterKeyId'] in [
                u'alias/aws/sqs',
                u'arn:aws:kms:us-east-1:644160558196:key/8785aeb9-a616-4e2b-bbd3-df3cde76bcc5'
            ])
            self.assertTrue(r['QueueArn'] in [
                u'arn:aws:sqs:us-east-1:644160558196:sqs-test-alias',
                u'arn:aws:sqs:us-east-1:644160558196:sqs-test-id'
            ])

    def test_sqs_post_finding(self):
        factory = self.replay_flight_data('test_sqs_post_finding')
        p = self.load_policy({
            'name': 'sqs',
            'resource': 'aws.sqs',
            'actions': [
                {'type': 'post-finding',
                 'types': [
                     'Software and Configuration Checks/OrgStandard/abc-123']}]},
            session_factory=factory, config={'region': 'us-west-2'})
        queues = p.resource_manager.get_resources([
            'test_sqs_modify_policy_add_remove_statements'])
        post_finding = p.resource_manager.actions[0]
        rfinding = post_finding.format_resource(queues[0])

        assert rfinding == {'Details': {
            'AwsSqsQueue': {
                'KmsDataKeyReusePeriodSeconds': 300,
                'KmsMasterKeyId': 'alias/aws/sqs',
                'QueueName': 'test_sqs_modify_policy_add_remove_statements'}},
            'Id': 'arn:aws:sqs:us-west-2:644160558196:test_sqs_modify_policy_add_remove_statements',
            'Partition': 'aws',
            'Region': 'us-west-2',
            'Type': 'AwsSqsQueue'}
        shape_validate(
            rfinding['Details']['AwsSqsQueue'],
            'AwsSqsQueueDetails',
            'securityhub',
        )

    def test_sqs_access_analyzer_parameterized(self):
        factory = self.replay_flight_data('test_sqs_analyzer_finding')
        p = self.load_policy({
            'name': 'check-sqs',
            'resource': 'aws.sqs',
            'filters': [
                {'QueueUrl': 'https://queue.amazonaws.com/644160558196/public-test'},
                {'type': 'iam-analyzer',
                 'key': 'analyzedAt',
                 'value_type': 'age',
                 'value': 5,
                 'op': 'gt'},
            ]
        }, session_factory=factory)
        resources = p.run()
        self.assertEqual(len(resources), 1)
        self.assertIn('c7n:AccessAnalysis', resources[0])

    def test_sqs_access_analyzer(self):
        factory = self.replay_flight_data('test_sqs_analyzer_finding')
        p = self.load_policy({
            'name': 'check-sqs',
            'resource': 'aws.sqs',
            'filters': [
                {'QueueUrl': 'https://queue.amazonaws.com/644160558196/public-test'},
                {'type': 'iam-analyzer'}
            ]
        }, session_factory=factory)
        resources = p.run()
        self.assertEqual(len(resources), 1)
        self.assertIn('c7n:AccessAnalysis', resources[0])
