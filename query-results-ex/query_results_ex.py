import hashlib
import json
import logging
import re
import sqlite3

import pystache

from redash import models
from redash.permissions import has_access, not_view_only
from redash.query_runner import *
from redash.query_runner.query_results import guess_type, fix_column_name
from redash.utils import JSONEncoder

logger = logging.getLogger(__name__)


class PermissionError(Exception):
    pass


def extract_query_ids_and_params(query):
    queries = re.findall(r'(?:join|from)\s+(query_(\d+)(?:\(([^)]+)\))?)', query, re.IGNORECASE)

    for i, q in enumerate(queries):
        queries[i] = [q[0], q[1], _parse_query_params(q[2])]

    return [q[0] for q in queries], [int(q[1]) for q in queries], [dict(q[2]) for q in queries]


def _load_query(user, query_id):
    query = models.Query.get_by_id(query_id)

    if user.org_id != query.org_id:
        raise PermissionError('Query id {} not found.'.format(query.id))

    if not has_access(query.data_source.groups, user, not_view_only):
        raise PermissionError(u'You are not allowed to execute queries on {} data source (used for query id {}).'.format(
            query.data_source.name, query.id))

    return query


def _parse_query_params(params_str):
    if params_str == '':
        return {}

    params = {}
    for param_str in params_str.strip().split(','):
        k, v = tuple(param_str.strip().split('='))
        params[k] = v

    return params


def create_tables_from_query_ids(user, connection, tables, query_ids, query_params):
    for i, query_id in enumerate(query_ids):
        query = _load_query(user, query_id)

        query_text = pystache.render(query.query_text, query_params[i])
        results, error = query.data_source.query_runner.run_query(query_text, user)

        if error:
            raise Exception('Failed loading results for query id {}.'.format(query.id))

        results = json.loads(results)
        tmp_table_name = 'query_{}'.format(hashlib.md5(tables[i].encode("utf8")).hexdigest()[0:8])
        create_table(connection, tmp_table_name, results)


def create_table(connection, table_name, query_results):
    columns = [column['name']
               for column in query_results['columns']]
    safe_columns = [fix_column_name(column) for column in columns]

    column_list = ', '.join(safe_columns)
    create_table = u'CREATE TABLE {table_name} ({column_list})'.format(
        table_name=table_name, column_list=column_list)
    logger.debug('CREATE TABLE query: %s', create_table)
    connection.execute(create_table)

    insert_template = u'insert into {table_name} ({column_list}) values ({place_holders})'.format(
        table_name=table_name,
        column_list=column_list,
        place_holders=','.join(['?'] * len(columns)))

    for row in query_results['rows']:
        values = [row.get(column) for column in columns]
        connection.execute(insert_template, values)


class ResultsEx(BaseQueryRunner):
    should_annotate_query = False
    noop_query = 'SELECT 1'

    @classmethod
    def configuration_schema(cls):
        return {
            'type': 'object',
            'properties': {},
        }

    @classmethod
    def name(cls):
        return 'Query Results Ex (Unofficial)'

    def run_query(self, query, user):
        connection = sqlite3.connect(':memory:')

        tables, query_ids, query_params = extract_query_ids_and_params(query)
        create_tables_from_query_ids(user, connection, tables, query_ids, query_params)

        cursor = connection.cursor()

        try:
            for i, table in enumerate(tables):
                tmp_table_name = 'query_{}'.format(hashlib.md5(tables[i].encode("utf8")).hexdigest()[0:8])
                query = query.replace(table, tmp_table_name, 1)
            cursor.execute(query)

            if cursor.description is not None:
                columns = self.fetch_columns(
                    [(i[0], None) for i in cursor.description])

                rows = []
                column_names = [c['name'] for c in columns]

                for i, row in enumerate(cursor):
                    for j, col in enumerate(row):
                        guess = guess_type(col)

                        if columns[j]['type'] is None:
                            columns[j]['type'] = guess
                        elif columns[j]['type'] != guess:
                            columns[j]['type'] = TYPE_STRING

                    rows.append(dict(zip(column_names, row)))

                data = {'columns': columns, 'rows': rows}
                error = None
                json_data = json.dumps(data, cls=JSONEncoder)
            else:
                error = 'Query completed but it returned no data.'
                json_data = None
        except KeyboardInterrupt:
            connection.cancel()
            error = 'Query cancelled by user.'
            json_data = None
        finally:
            connection.close()

        return json_data, error


register(ResultsEx)
