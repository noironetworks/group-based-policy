# Copyright (c) 2020 Cisco Systems Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

# REVISIT: Eliminate this module as soon as definitions from
# neutron.db.api, which is eliminated in stein, are no longer
# needed. Please DO NOT add any definition to this module that is not
# a direct alias of a definion in the version of neutron_lib.db.api
# corresponding to the newest neutron branch supported by this
# repository.

import sqlalchemy

from neutron.db import _model_query
from neutron.db import _utils as db_utils
from neutron.db import api as old_api
import neutron.objects.base as n_base
from neutron_lib.db import api

get_by_id = _model_query.get_by_id
get_collection = _model_query.get_collection
get_collection_count = _model_query.get_collection_count
get_collection_query = _model_query.get_collection_query
get_context_manager = api.get_context_manager
get_marker_obj = db_utils.get_marker_obj
get_reader_session = api.get_reader_session
get_writer_session = api.get_writer_session
is_retriable = old_api.is_retriable
resource_fields = db_utils.resource_fields
retry_db_errors = old_api.retry_db_errors
retry_if_session_inactive = old_api.retry_if_session_inactive
CONTEXT_READER = get_context_manager().reader
CONTEXT_WRITER = get_context_manager().writer


def get_session_from_obj(db_obj):
    # Support OVOs
    if isinstance(db_obj, n_base.NeutronObject):
        return db_obj.obj_context.session
    try:
        instance = sqlalchemy.inspect(db_obj)
        return instance.session
    except Exception:
        return None
