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

from neutron_lib.db import model_base
from oslo_log import log
import sqlalchemy as sa
from sqlalchemy.ext import baked

LOG = log.getLogger(__name__)

BAKERY = baked.bakery(_size_alert=lambda c: LOG.warning(
    "sqlalchemy baked query cache size exceeded in %s", __name__))


class ApicReuseBdDB(model_base.BASEV2):
    __tablename__ = 'gp_apic_mapping_reuse_bds'
    l2_policy_id = sa.Column(
        sa.String(36), sa.ForeignKey('gp_l2_policies.id',
                                     ondelete='CASCADE'), primary_key=True)
    target_l2_policy_id = sa.Column(
        sa.String(36), sa.ForeignKey('gp_l2_policies.id'), nullable=False)


class ApicReuseBdDBMixin(object):

    def get_reuse_bd_l2policy(self, session, l2_policy_id):
        # REVISIT: This method is not executed in any unit test.

        query = BAKERY(lambda s: s.query(
            ApicReuseBdDB))
        query += lambda q: q.filter_by(
            l2_policy_id=sa.bindparam('l2_policy_id'))
        row = query(session).params(
            l2_policy_id=l2_policy_id).first()

        return row

    def add_reuse_bd_l2policy(self, session, l2_policy_id,
                              target_l2_policy_id):
        with session.begin(subtransactions=True):
            row = ApicReuseBdDB(l2_policy_id=l2_policy_id,
                                target_l2_policy_id=target_l2_policy_id)
            session.add(row)

    def is_reuse_bd_target(self, session, l2_policy_id):
        # REVISIT: This method is not executed in any unit test.

        query = BAKERY(lambda s: s.query(
            ApicReuseBdDB))
        query += lambda q: q.filter_by(
            target_l2_policy_id=sa.bindparam('l2_policy_id'))
        return query(session).params(
            l2_policy_id=l2_policy_id).first() is not None
