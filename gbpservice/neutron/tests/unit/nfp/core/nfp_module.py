#  Licensed under the Apache License, Version 2.0 (the "License"); you may
#  not use this file except in compliance with the License. You may obtain
#  a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#  WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#  License for the specific language governing permissions and limitations
#  under the License.from gbpservice.neutron.nsf.core import main

from gbpservice.nfp.core import context as nfp_context
from gbpservice.nfp.core import event
from gbpservice.nfp.core import module as nfp_api

"""An example NFP module used for UT.

    Implements a sample NFP module used by UT code.
    Event handlers for the events generated by UT
    code are implemented here.
"""


class EventsHandler(nfp_api.NfpEventHandler):

    def __init__(self, controller):
        self.controller = controller

    def handle_event(self, event):
        event.context['log_context']['namespace'] = event.desc.target
        nfp_context.init(event.context)

        if event.id == 'TEST_EVENT_ACK_FROM_WORKER':
            self.controller.event_ack_handler_cb_obj.set()

        if event.id == 'TEST_POST_EVENT_FROM_WORKER':
            self.controller.post_event_worker_wait_obj.set()

        if event.id == 'TEST_POLL_EVENT_FROM_WORKER':
            self.controller.poll_event_worker_wait_obj.set()
            self.controller.poll_event(event, spacing=1)

        if event.id == 'TEST_POLL_EVENT_CANCEL_FROM_WORKER':
            self.controller.poll_event_worker_wait_obj.set()
            self.controller.poll_event(event, spacing=1, max_times=2)

    def handle_poll_event(self, event):
        if event.id == 'TEST_POLL_EVENT_FROM_WORKER':
            self.controller.poll_event_poll_wait_obj.set()
        if event.id == 'TEST_POLL_EVENT_CANCEL_FROM_WORKER':
            self.controller.poll_event_poll_wait_obj.set()

    def event_cancelled(self, event, reason):
        if event.id == 'TEST_POLL_EVENT_CANCEL_FROM_WORKER':
            if reason == 'MAX_TIMED_OUT':
                self.controller.poll_event_poll_cancel_wait_obj.set()

    @nfp_api.poll_event_desc(event='POLL_EVENT_DECORATOR', spacing=2)
    def handle_poll_event_desc(self, event):
        pass


def nfp_module_post_init(controller, conf):
    if hasattr(controller, 'nfp_module_post_init_wait_obj'):
        controller.nfp_module_post_init_wait_obj.set()


def nfp_module_init(controller, conf):
    if hasattr(controller, 'nfp_module_init_wait_obj'):
        controller.nfp_module_init_wait_obj.set()

    evs = [
        event.Event(id='EVENT_1', handler=EventsHandler(controller)),
        event.Event(id='EVENT_LOAD_1', handler=EventsHandler(controller)),
        event.Event(id='EVENT_LOAD_2', handler=EventsHandler(controller)),
        event.Event(id='EVENT_LOAD_3', handler=EventsHandler(controller)),
        event.Event(id='EVENT_LOAD_4', handler=EventsHandler(controller)),
        event.Event(id='EVENT_LOAD_5', handler=EventsHandler(controller)),
        event.Event(id='EVENT_LOAD_6', handler=EventsHandler(controller)),
        event.Event(id='EVENT_LOAD_7', handler=EventsHandler(controller)),
        event.Event(id='EVENT_LOAD_8', handler=EventsHandler(controller)),
        event.Event(id='EVENT_LOAD_9', handler=EventsHandler(controller)),
        event.Event(id='EVENT_LOAD_10', handler=EventsHandler(controller)),
        event.Event(id='SEQUENCE_EVENT_1', handler=EventsHandler(controller)),
        event.Event(id='SEQUENCE_EVENT_2', handler=EventsHandler(controller)),
        event.Event(id='POLL_EVENT', handler=EventsHandler(controller)),
        event.Event(id='POLL_EVENT_DECORATOR',
                    handler=EventsHandler(controller)),
        event.Event(id='POLL_EVENT_WITHOUT_SPACING',
                    handler=EventsHandler(controller)),
        event.Event(id='TEST_EVENT_ACK_FROM_WORKER',
                    handler=EventsHandler(controller)),
        event.Event(id='TEST_POST_EVENT_FROM_WORKER',
                    handler=EventsHandler(controller)),
        event.Event(id='TEST_POLL_EVENT_FROM_WORKER',
                    handler=EventsHandler(controller)),
        event.Event(id='TEST_POLL_EVENT_CANCEL_FROM_WORKER',
                    handler=EventsHandler(controller))
    ]
    controller.register_events(evs)
