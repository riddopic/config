# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2012 Red Hat, Inc.
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

from eventlet import greenlet
from eventlet import greenpool
from eventlet import greenthread

from oslo_log import log as logging
from oslo_service import loopingcall

LOG = logging.getLogger(__name__)


def _thread_done(gt, *args, **kwargs):
    """ Callback function to be passed to GreenThread.link() when we spawn()
    Calls the :class:`ThreadGroup` to notify if.

    """
    kwargs['group'].thread_done(kwargs['thread'])


class Thread(object):
    """ Wrapper around a greenthread, that holds a reference to the
    :class:`ThreadGroup`. The Thread will notify the :class:`ThreadGroup` when
    it has done so it can be removed from the threads list.
    """
    def __init__(self, thread, group):
        self.thread = thread
        self.thread.link(_thread_done, group=group, thread=self)

    def stop(self):
        self.thread.kill()

    def wait(self):
        return self.thread.wait()


class ThreadGroup(object):
    """ The point of the ThreadGroup classis to:

    * keep track of timers and greenthreads (making it easier to stop them
      when need be).
    * provide an easy API to add timers.
    """
    def __init__(self, thread_pool_size=10):
        self.pool = greenpool.GreenPool(thread_pool_size)
        self.threads = []
        self.timers = []

    def add_dynamic_timer(self, callback, initial_delay=None,
                          periodic_interval_max=None, *args, **kwargs):
        timer = loopingcall.DynamicLoopingCall(callback, *args, **kwargs)
        timer.start(initial_delay=initial_delay,
                    periodic_interval_max=periodic_interval_max)
        self.timers.append(timer)

    def add_timer(self, interval, callback, initial_delay=None,
                  *args, **kwargs):
        pulse = loopingcall.FixedIntervalLoopingCall(callback, *args, **kwargs)
        pulse.start(interval=interval,
                    initial_delay=initial_delay)
        self.timers.append(pulse)

    def add_thread(self, callback, *args, **kwargs):
        gt = self.pool.spawn(callback, *args, **kwargs)
        th = Thread(gt, self)
        self.threads.append(th)

    def thread_done(self, thread):
        self.threads.remove(thread)

    def stop(self):
        current = greenthread.getcurrent()
        for x in self.threads:
            if x is current:
                # don't kill the current thread.
                continue
            try:
                x.stop()
            except Exception as ex:
                LOG.exception(ex)

        for x in self.timers:
            try:
                x.stop()
            except Exception as ex:
                LOG.exception(ex)
        self.timers = []

    def wait(self):
        for x in self.timers:
            try:
                x.wait()
            except greenlet.GreenletExit:
                pass
            except Exception as ex:
                LOG.exception(ex)
        current = greenthread.getcurrent()
        for x in self.threads:
            if x is current:
                continue
            try:
                x.wait()
            except greenlet.GreenletExit:
                pass
            except Exception as ex:
                LOG.exception(ex)
