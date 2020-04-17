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

from neutron_lib.db import api

get_context_manager = api.get_context_manager
get_reader_session = api.get_reader_session
get_writer_session = api.get_writer_session
is_retriable = api.is_retriable
retry_db_errors = api.retry_db_errors
retry_if_session_inactive = api.retry_if_session_inactive
CONTEXT_READER = api.CONTEXT_READER
CONTEXT_WRITER = api.CONTEXT_WRITER
