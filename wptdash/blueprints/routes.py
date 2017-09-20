#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
    WPTDash
    ~~~~~~~

    An application that consolidates pull request build information into
    a single GitHub comment and provides an interface for displaying
    more detailed forms of that information.
"""
import configparser
from datetime import date, datetime, timedelta
from flask import Blueprint, g, render_template, request
from jsonschema import validate
from sqlalchemy import or_
import hashlib
import hmac
import json
import math
import re
import shlex
from urllib.parse import parse_qs

from wptdash.commenter import update_github_comment
from wptdash.github import GitHub
from wptdash.travis import Travis

CONFIG = configparser.ConfigParser()
CONFIG.readfp(open(r'config.txt'))
GH_TOKEN = CONFIG.get('GitHub', 'GH_TOKEN')
GH_WEBHOOK_TOKEN = CONFIG.get('GitHub', 'GH_WEBHOOK_TOKEN')
ORG = CONFIG.get('GitHub', 'ORG')
REPO = CONFIG.get('GitHub', 'REPO')
DATETIME_FORMAT = '%Y-%m-%dT%H:%M:%SZ'

RE_ENV = re.compile(r'(\w+)=(.+)')
RE_SAUCE = re.compile(r'^sauce:')

bp = Blueprint('routes', __name__)


@bp.route('/')
def main():
    db = g.db
    models = g.models
    pulls = db.session.query(models.PullRequest).order_by(
        models.PullRequest.created_at.desc()
    ).limit(100).all()
    return render_template('index.html', pulls=pulls)


@bp.route('/pull/<int:pull_number>')
def pull_detail(pull_number):
    db = g.db
    models = g.models
    pull = models.get(db.session, models.PullRequest, number=pull_number)
    return render_template('pull.html', pull=pull, pull_number=pull_number)


@bp.route('/build/<int:build_number>')
def build_detail(build_number):
    db = g.db
    models = g.models
    build = models.get(db.session, models.Build, number=build_number)
    return render_template('build.html', build=build, build_number=build_number,
                           org_name=ORG, repo_name=REPO)


@bp.route('/job/<string:job_number>')
def job_detail(job_number):
    db = g.db
    models = g.models
    job = models.get(db.session, models.Job, number=job_number)
    return render_template('job.html', job=job, job_number=job_number,
                           org_name=ORG, repo_name=REPO)


@bp.route('/performance')
def performance_metrics():

    def get_quarter_start_date(quarter, year):
        quarter_first_month = quarter * 3 - 2
        quarter_start = date(year, quarter_first_month, 1)
        return quarter_start

    def get_default_start_end():
        month_end_days = {
            3: 31,
            6: 30,
            9: 30,
            12: 31
        }
        today = date.today()
        month = today.month
        year = today.year
        quarter = math.ceil(month / 3)
        quarter_start = get_quarter_start_date(quarter, year)
        delta = today - quarter_start
        if delta.days < 45:
            quarter -= 1
            if quarter == 0:
                quarter = 4
                year -= 1
            quarter_start = get_quarter_start_date(quarter, year)
        quarter_end_month = quarter_start.month + 2
        quarter_end = date(year, quarter_end_month,
                           month_end_days[quarter_end_month])
        return quarter_start, quarter_end

    db = g.db
    models = g.models
    (default_start, default_end) = get_default_start_end()
    start_date = request.args.get('start') or default_start
    end_date = request.args.get('end') or default_end
    okr_delta = timedelta(minutes=30)
    total_okr = 0

    jobs = db.session.query(models.Job).filter(
        models.Job.product_id == models.Product.id
    ).filter(
        models.Job.build_id == models.Build.id
    ).filter(
        models.Build.pull_request_id == models.PullRequest.id
    ).filter(
        or_(models.Product.name.ilike('%firefox%'),
            models.Product.name.ilike('%chrome%'),
            models.Product.name.ilike('%safari%'),
            models.Product.name.ilike('%microsoft%'))
    ).filter(models.PullRequest.created_at >= datetime.strptime(
        '%sT00:00:00Z' % start_date, DATETIME_FORMAT
    ), models.PullRequest.created_at < datetime.strptime(
        '%sT00:00:00Z' % end_date, DATETIME_FORMAT
    )).order_by(
        models.PullRequest.created_at.asc()
    ).all()

    for job in jobs:
        job_finished_at = job.finished_at or job.build.finished_at
        job_delta = job_finished_at - job.build.pull_request.created_at
        job_okr = job_delta <= okr_delta and job.state.name in [
            'PASSED', 'FAILED', 'FINISHED'
        ]
        total_okr += 1.0 if job_okr else 0.0
    total_okr = total_okr / len(jobs)

    return render_template('performance.html', jobs=jobs,
                           start=start_date, end=end_date,
                           okr_delta=okr_delta, total_okr=total_okr)


@bp.route('/api/pull', methods=['POST'])
def add_pull_request():
    is_authorized = validate_hmac_signature(request.data, request.headers.get('X_HUB_SIGNATURE'))

    if not is_authorized:
        return 'Invalid Authorization Signature.', 401

    db = g.db
    models = g.models
    schema = {
        '$schema': 'http://json-schema.org/schema#',
        'title': 'Pull Request Event',
        'type': 'object',
        'properties': {
            'pull_request': {
                'type': 'object',
            },
        },
        'required': ['pull_request'],
    }
    data = request.get_json(force=True)
    validate(data, schema)

    pr = add_pr_to_session(data['pull_request'], db, models)

    route_response = update_github_comment(pr)
    db.session.commit()
    return route_response


@bp.route('/api/build', methods=['POST'])
def add_build():
    db = g.db
    models = g.models
    schema = {
        '$schema': 'http://json-schema.org/schema#',
        'title': 'Travis Build Event',
        'type': 'object',
        'definitions': {
            'date_time': {
                'type': 'string',
                'format': 'date-time',
            },
        },
        'properties': {
            'id': {'type': 'integer'},
            'number': {'type': 'string'},
            'head_commit': {'type': 'string'},
            'base_commit': {'type': 'string'},
            'pull_request': {'type': 'boolean'},
            'pull_request_number': {'oneOf': [
                {'type': 'integer'},
                {'type': 'null'},
            ]},
            'status_message': {
                'enum': ['Pending', 'Passed', 'Fixed', 'Broken', 'Failed',
                         'Still Failing', 'Canceled', 'Errored'],
            },
            'started_at': {'$ref': '#/definitions/date_time'},
            'finished_at': {'$ref': '#/definitions/date_time'},
            'repository': {
                'type': 'object',
                'properties': {
                    'name': {'type': 'string'},
                    'owner_name': {'type': 'string'},
                },
                'required': ['name', 'owner_name'],
            },
            'matrix': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'id': {'type': 'integer'},
                        'number': {'type': 'string'},
                        'state': {
                            'type': 'string',
                            'enum': ['created', 'queued', 'started', 'passed',
                                     'failed', 'errored', 'finished']
                        },
                        'started_at': {'oneOf': [
                            {'$ref': '#/definitions/date_time'},
                            {'type': 'null'},
                        ]},
                        'finished_at': {'oneOf': [
                            {'$ref': '#/definitions/date_time'},
                            {'type': 'null'},
                        ]},
                        'allow_failure': {'type': 'boolean'},
                        'config': {'type': 'object'},
                    },
                    'required': ['id', 'number', 'state', 'started_at',
                                 'config', 'allow_failure']
                }
            }
        },
        'required': ['id', 'number', 'head_commit', 'base_commit',
                     'pull_request', 'pull_request_number', 'status',
                     'repository'],
    }

    travis = Travis()

    # The payload comes in the request, but we need to make sure it is
    # really signed by Travis CI. If not, respond to this request with
    # an error.
    resp = validate(json.loads(request.form['payload']), schema)

    verified_payload = travis.get_verified_payload(
        request.form['payload'], request.headers['SIGNATURE']
    )
    error = verified_payload.get('error')
    if error:
        return error.get('message'), error.get('code')

    # Ensure only builds for this repository can post here.
    repository = verified_payload.get("repository")
    owner_name = repository.get("owner_name")
    repo_name = repository.get("name")
    if owner_name != ORG or repo_name != REPO:
        return "Forbidden: Repository Mismatch. Build for %s/%s attempting to comment on %s/%s" % (owner_name, repo_name, ORG, REPO), 403

    pr_number = verified_payload['pull_request_number']

    pr = models.get(
        db.session, models.PullRequest,
        number=pr_number,
    )

    if not pr:
        github = GitHub()
        pr_data = github.get_pr(pr_number)
        pr = add_pr_to_session(pr_data, db, models)

    head_commit, _ = models.get_or_create(
        db.session, models.Commit,
        sha=verified_payload['head_commit']
    )

    base_commit, _ = models.get_or_create(
        db.session, models.Commit,
        sha=verified_payload['base_commit']
    )

    build, _ = models.get_or_create(
        db.session, models.Build, id=verified_payload['id']
    )
    build.number = int(verified_payload['number'])
    build.pull_request = pr
    build.head_commit = head_commit
    build.base_commit = base_commit
    build.status = models.BuildStatus.from_string(
        verified_payload['status_message']
    )
    if verified_payload['started_at']:
        build.started_at = datetime.strptime(
            verified_payload['started_at'], DATETIME_FORMAT
        )

    if verified_payload['finished_at']:
        build.finished_at = datetime.strptime(
            verified_payload['finished_at'], DATETIME_FORMAT
        )

    for job_data in verified_payload['matrix']:
        add_job_to_session(job_data, build, db, models)

    route_response = update_github_comment(pr)
    db.session.commit()
    return route_response


@bp.route('/api/test-mirror', methods=['POST', 'DELETE'])
def update_test_mirror():
    db = g.db
    models = g.models
    schema = None

    if request.method == 'DELETE':
        schema = {
            '$schema': 'http://json-schema.org/schema#',
            'title': 'PR Mirrored Event',
            'type': 'object',
            'properties': {
                'issue_number': {'type': 'integer'},
            },
            'required': ['issue_number', 'url'],
        }
    else:
        schema = {
            '$schema': 'http://json-schema.org/schema#',
            'title': 'PR Mirrored Event',
            'type': 'object',
            'properties': {
                'issue_number': {'type': 'integer'},
                'url': {'type': 'string'}
            },
            'required': ['issue_number', 'url'],
        }

    data = request.get_json(force=True)
    validate(data, schema)

    pr = models.get(
        db.session, models.PullRequest, number=data['issue_number']
    )

    if not pr:
        return 'Pull request data for this mirror does not exist in the database.', 422

    pr.mirror = pr.mirror or models.TestMirror()
    pr.mirror.url = data['url'] if request.method == 'POST' else None

    route_response = update_github_comment(pr)
    db.session.commit()
    return route_response


@bp.route('/api/stability', methods=['POST'])
def add_stability_check():
    db = g.db
    models = g.models
    schema = {
        'type': 'object',
        'properties': {
            'pull': {
                'type': 'object',
                'properties': {
                    'number': {'type': 'integer'},
                    'sha': {'type': 'string'},
                },
                'required': ['number', 'sha'],
            },
            'job': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'integer'},
                    'number': {'type': 'string'},
                    'allow_failure': {'type': 'boolean'},
                    'status': {
                        'type': 'string',
                        'enum': ['created', 'queued', 'started', 'passed',
                                 'failed', 'errored', 'finished']
                    },
                },
                'required': [
                    'id', 'number', 'allow_failure', 'status',
                ],
            },
            'build': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'integer'},
                    'number': {'type': 'string'},
                },
                'required': [
                    'id', 'number',
                ],
            },
            'product': {
                'type': 'string',
                'maxLength': 255,
            },
            'iterations': {
                'type': 'integer'
            },
            'message': {
                'type': 'string'
            },
            'results': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'result': {
                            'type': 'object',
                            'properties': {
                                'status': {
                                    'type': 'object',
                                    'patternProperties': {
                                        '^(?:pass|fail|ok|timeout|error|notrun|crash)$': {
                                            'type': 'integer'
                                        },
                                    },
                                },
                                'subtests': {
                                    'type': 'array',
                                    'items': {
                                        'type': 'object',
                                        'properties': {
                                            'result': {
                                                'type': 'object',
                                                'properties': {
                                                    'status': {
                                                        'type': 'object',
                                                        'patternProperties': {
                                                            '^(?:pass|fail|ok|timeout|error|notrun|crash)$': {
                                                                'type': 'integer'
                                                            },
                                                        },
                                                    },
                                                    'messages': {
                                                        'type': 'array',
                                                        'items': {
                                                            'type': 'string'
                                                        },
                                                    },
                                                },
                                                'required': ['status', 'messages'],
                                            },
                                            'test': {
                                                'type': 'string',
                                            },
                                        },
                                        'required': ['result', 'test'],
                                    },
                                },
                            },
                            'required': ['status'],
                        },
                        'test': {
                            'type': 'string',
                        },
                    },
                    'required': ['test', 'result'],
                },
            },
        },
        'required': ['pull', 'job', 'build', 'product', 'iterations',
                     'results']
    }

    data = request.get_json(force=True)
    validate(data, schema)

    pr_number = data['pull']['number']

    pr = models.get(
        db.session, models.PullRequest,
        number=pr_number,
    )

    if not pr:
        github = GitHub()
        pr_data = github.get_pr(pr_number)
        pr = add_pr_to_session(pr_data, db, models)

    build, _ = models.get_or_create(
        db.session, models.Build, id=data['build']['id']
    )
    build.number = int(data['build']['number'])
    build.pull_request = pr
    build.head_sha = data['pull']['sha']
    build.status = build.status or models.BuildStatus.from_string('pending')

    product_name = normalize_product_name(data['product'])
    product, _ = models.get_or_create(
        db.session, models.Product, name=product_name
    )

    job, _ = models.get_or_create(
        db.session, models.Job, id=data['job']['id']
    )
    job.number = data['job']['number']
    job.allow_failure = data['job']['allow_failure']
    job.build = build
    job.product = product
    job.message = data.get('message', None)
    job.state = models.JobStatus.from_string(data['job']['status'])

    for test_data in data.get('results', []):
        test, _ = models.get_or_create(
            db.session,
            models.Test,
            id=test_data['test']
        )

        test_result, _ = models.get_or_create(
            db.session,
            models.JobResult,
            test_id=test.id,
            job_id=job.id,
        )
        test_result.iterations = data['iterations']
        test_result.consistent = True

        for status_name, count in test_data['result']['status'].items():
            status, _ = models.get_or_create(
                db.session,
                models.StabilityStatus,
                job_id=job.id,
                test_id=test.id,
                status=models.TestStatus.from_string(status_name)
            )
            status.count = count
            if (count < data['iterations']):
                test_result.consistent = False

        for subtest_data in test_data['result'].get('subtests', []):
            subtest, _ = models.get_or_create(
                db.session,
                models.Test,
                id=subtest_data['test']
            )
            subtest.parent = test

            subtest_result, _ = models.get_or_create(
                db.session,
                models.JobResult,
                test_id=subtest.id,
                job_id=job.id,
            )
            subtest_result.iterations = data['iterations']
            subtest_result.messages = json.dumps(subtest_data['result']['messages'])
            subtest_result.consistent = True

            for subtest_status_name, count in subtest_data['result']['status'].items():
                subtest_status, _ = models.get_or_create(
                    db.session,
                    models.StabilityStatus,
                    job_id=job.id,
                    test_id=subtest.id,
                    status=models.TestStatus.from_string(subtest_status_name)
                )
                subtest_status.count = count
                if (count < data['iterations']):
                    subtest_result.consistent = False
                    test_result.consistent = False

    route_response = update_github_comment(pr)
    db.session.commit()
    return route_response


def normalize_product_name(product_name):
    return RE_SAUCE.sub('', product_name) if product_name else None


def dictify_env_list(env_list):
    env_dict = {}
    for env_string in env_list:
        env_vars = shlex.split(env_string)
        for variable in env_vars:
            match = RE_ENV.match(variable)
            if match:
                env_dict[match.group(1)] = match.group(2)
    return env_dict


def add_job_to_session(job_data, build, db, models):
    env_dict = dictify_env_list(job_data['config'].get('env', []))

    product_name = normalize_product_name(env_dict.get('PRODUCT'))
    job_name = env_dict.get('JOB')
    python_version = env_dict.get('TOXENV')

    if not product_name:
        product_name = job_name
        if python_version:
            product_name += ' in %s' % python_version

    product, _ = models.get_or_create(
        db.session, models.Product, name=product_name
    )
    job, _ = models.get_or_create(
        db.session, models.Job, id=job_data['id']
    )
    job.number = job_data['number']
    job.build = build
    job.product = product

    if job_data['status'] == 0:
        job.state = models.JobStatus.PASSED
    else:
        job.state = models.JobStatus.FAILED
    job.allow_failure = job_data['allow_failure']

    if job_data['started_at']:
        job.started_at = datetime.strptime(
            job_data['started_at'], DATETIME_FORMAT
        )
    if job_data['finished_at']:
        job.finished_at = datetime.strptime(
            job_data['finished_at'], DATETIME_FORMAT
        )


def add_pr_to_session(pr_data, db, models):
    db = g.db
    models = g.models
    schema = {
        '$schema': 'http://json-schema.org/schema#',
        'title': 'Pull Request',
        'definitions': {
            'commit_object': {
                'type': 'object',
                'properties': {
                    'ref': {'type': 'string'},
                    'sha': {'type': 'string'},
                    'user': {'$ref': '#/definitions/github_user'},
                    'repo': {
                        'type': 'object',
                        'properties': {
                            'id': {'type': 'integer'},
                            'name': {'type': 'string'},
                            'owner': {'$ref': '#/definitions/github_user'},
                        },
                        'required': ['id', 'owner'],
                    },
                },
                'required': ['sha', 'ref', 'user', 'repo']
            },
            'date_time': {
                'type': 'string',
                'format': 'date-time',
            },
            'github_user': {
                'type': 'object',
                'properties': {
                    'login': {'type': 'string'},
                    'id': {'type': 'integer'},
                },
                'required': ['login', 'id'],
            },
        },
        'type': 'object',
        'properties': {
            'id': {'type': 'integer'},
            'number': {'type': 'integer'},
            'title': {'type': 'string'},
            'user': {'$ref': '#/definitions/github_user'},
            'merged': {'type': 'boolean'},
            'state': {
                'enum': ['open', 'closed'],
            },
            'head': {'$ref': '#/definitions/commit_object'},
            'base': {'$ref': '#/definitions/commit_object'},
            'merged_by': {'oneOf': [
                {'$ref': '#definitions/github_user'},
                {'type': 'null'},
            ]},
            'created_at': {'$ref': '#/definitions/date_time'},
            'updated_at': {'$ref': '#/definitions/date_time'},
            'closed_at': {'oneOf': [
                {'$ref': '#/definitions/date_time'},
                {'type': 'null'},
            ]},
            'merged_at': {'oneOf': [
                {'$ref': '#/definitions/date_time'},
                {'type': 'null'},
            ]},
        },
        'required': [
            'id', 'number', 'title', 'user', 'merged', 'state', 'head',
            'base', 'created_at', 'updated_at'
        ],
    }

    validate(pr_data, schema)

    pr_head = pr_data['head']
    pr_base = pr_data['base']
    merger = None

    creator, _ = models.get_or_create(
        db.session, models.GitHubUser, id=pr_data['user']['id']
    )
    creator.login = pr_data['user']['login']

    if pr_data['merged_by']:
        merger, _ = models.get_or_create(
            db.session, models.GitHubUser,
            id=pr_data['merged_by']['id']
        )
        merger.login = pr_data['merged_by']['login']

    head_commit_user, _ = models.get_or_create(
        db.session, models.GitHubUser,
        id=pr_head['user']['id']
    )
    head_commit_user.login = pr_head['user']['login']

    head_commit, _ = models.get_or_create(
        db.session, models.Commit,
        sha=pr_head['sha']
    )
    head_commit.user = head_commit_user

    base_commit_user, _ = models.get_or_create(
        db.session, models.GitHubUser,
        id=pr_base['user']['id']
    )
    base_commit_user.login = pr_base['user']['login']

    base_commit, _ = models.get_or_create(
        db.session, models.Commit,
        sha=pr_base['sha']
    )
    base_commit.user = base_commit_user

    # Query by ID and update in case name or owner have changed
    head_repo_owner, _ = models.get_or_create(
        db.session, models.GitHubUser,
        id=pr_head['repo']['owner']['id']
    )
    head_repo_owner.login = pr_head['repo']['owner']['login']

    head_repo, _ = models.get_or_create(
        db.session, models.Repository,
        id=pr_head['repo']['id']
    )
    head_repo.name = pr_head['repo']['name']
    head_repo.owner = head_repo_owner

    base_repo_owner, _ = models.get_or_create(
        db.session, models.GitHubUser,
        id=pr_base['repo']['owner']['id']
    )
    base_repo_owner.login = pr_base['repo']['owner']['login']

    base_repo, _ = models.get_or_create(
        db.session, models.Repository,
        id=pr_base['repo']['id']
    )
    base_repo.name = pr_base['repo']['name']
    base_repo.owner = base_repo_owner

    pr, _ = models.get_or_create(
        db.session, models.PullRequest, id=pr_data['id']
    )

    pr.number = pr_data['number']
    pr.title = pr_data['title']
    pr.state = models.PRStatus.from_string(pr_data['state'])
    pr.creator = creator
    pr.created_at = datetime.strptime(
        pr_data['created_at'], DATETIME_FORMAT
    )
    pr.merged = pr_data['merged']
    pr.merger = merger
    pr.merged_at = datetime.strptime(
        pr_data['merged_at'], DATETIME_FORMAT
    ) if pr_data['merged_at'] else None
    pr.head_commit = head_commit
    pr.base_commit = base_commit
    pr.head_repository = head_repo
    pr.base_repository = base_repo
    pr.head_branch = pr_head['ref']
    pr.base_branch = pr_base['ref']
    pr.updated_at = datetime.strptime(
        pr_data['updated_at'], DATETIME_FORMAT
    )
    pr.closed_at = datetime.strptime(
        pr_data['closed_at'], DATETIME_FORMAT
    ) if pr_data['closed_at'] else None
    pr.mirror = models.TestMirror(url=None)

    return pr


def create_hmac_signature(payload_body):
    return 'sha1=%s' % hmac.new(bytes(GH_WEBHOOK_TOKEN, 'utf-8'), payload_body,
                                hashlib.sha1).hexdigest()


def validate_hmac_signature(payload_body, signature):
    return hmac.compare_digest(create_hmac_signature(payload_body), signature)
