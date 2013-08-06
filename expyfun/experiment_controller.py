"""TODO: add docstring
"""
# import logging
import time
import numpy as np
import platform
import os
from os import path as op
from functools import partial
if platform.platform == 'Windows':
    from tdt.util import connect_rpcox, connect_zbus
else:
    connect_rpcox = None
    connect_zbus = None
from psychopy import visual, core, data, event, sound, gui
from psychopy import logging as psylog
from psychopy.constants import FINISHED, STARTED, NOT_STARTED
from .utils import get_config, verbose


class ExperimentController(object):
    """Interface for hardware control (audio, buttonbox, eye tracker, etc.)

    Parameters
    ----------
    exp_name : str
        Name of the experiment.
    audio_controller : str | None
        Can be 'psychopy' or a TDT model (e.g., 'RM1' or 'RP2'). If None,
        the type will be read from the system configuration file.
    response_device : str | None
        Can be 'keyboard' or 'buttonbox'.  If None, the type will be read
        from the system configuration file.
    stim_rms : float
        The RMS amplitude that the stimuli were generated at (strongly
        recommended to be 0.01).
    stim_amp : float
        The desired dB SPL at which to play the stimuli.
    noise_amp : float
        The desired dB SPL at which to play the dichotic noise.
    output_dir : str | 'rawData'
        An absolute or relative path to a directory in which raw experiment
        data will be stored. If output_folder does not exist, it will be
        created.
    window_size : list | array | None
        Window size to use. If list or array, it must have two elements.
        If None, the default will be read from the system config,
        falling back to [1920, 1080] if no system config is found.
    screen_num : int | None
        Screen to use. If None, the default will be read from the system
        config, falling back to 0 if no system config is found.
    force_quit : str | None
        Keyboard key to utilize as an experiment force-quit button.
    verbose : bool, str, int, or None
        If not None, override default verbose level (see expyfun.verbose).

    Returns
    -------
    exp_controller : instance of ExperimentController
        The experiment control interface.

    Notes
    -----
    TODO: add eye tracker and EEG
    TODO: Deal with fullscreen=True in psychopy init, if we want less than
          fullscreen windows, it won't allow it currently (making window_size
          pointless)...
    """

    @verbose
    def __init__(self, exp_name, audio_controller=None, response_device=None,
                 stim_rms=0.01, stim_amp=65, noise_amp=-np.Inf,
                 output_dir='rawData', window_size=None, screen_num=None,
                 force_quit=['escape'], verbose=None):

        self._stim_amp = stim_amp
        self._noise_amp = noise_amp
        self._force_quit = force_quit
        self.t = None  # timestamp
        self.f = None  # frame_number

        # some parameters...
        bkgd_color = [-1, -1, -1]  # psychopy does RGB from -1 to 1
        root_dir = './'
        core.checkPygletDuringWait = False

        # dictionary for experiment metadata
        self.exp_info = {'participant': 'foo', 'session': '001',
                         'exp_name': exp_name, 'date': data.getDateStr()}

        # session start dialog
        session_dialog = gui.DlgFromDict(dictionary=self.exp_info,
                                         fixed=['exp_name', 'date'],
                                         title=exp_name)
        if session_dialog.OK is False:
            core.quit()  # user pressed cancel

        # initialize log file
        if not op.isdir(op.join(root_dir, output_dir)):
            os.mkdir(op.join(root_dir, output_dir))
        basename = op.join(root_dir, output_dir,
                           '{0}_{1}'.format(self.exp_info['participant'],
                                            self.exp_info['date']))
        psylog.LogFile(basename + '.log', level=psylog.INFO)
        psylog.console.setLevel(psylog.WARNING)

        # clocks
        self.master_clock = core.Clock()
        self.trial_clock = core.Clock()

        # list of trial components
        self.trial_components = []

        # response device
        if response_device is None:
            self.response_device = get_config('RESPONSE_DEVICE')
        else:
            self.response_device = response_device

        # audio setup
        if audio_controller is None:
            self.audio_controller = get_config('AUDIO_CONTROLLER')
        else:
            self.audio_controller = audio_controller
        if self.audio_controller == 'psychopy':
            psylog.info('Expyfun: Initializing PsychoPy audio')
            self.tdt = None
            self._fs = 22050  # TODO: maybe should be user-configurable
            self.audio = sound.Sound(np.zeros((1, 2)), sampleRate=self._fs)
            self.audio.setVolume(1)  # TODO: check this w/r/t stim_scaler
            self.trial_components.append(self.audio)
        else:
            psylog.info('Expyfun: Setting up TDT')
            self.tdt = TDTObject(self.audio_controller,
                                 get_config('TDT_CIRCUIT'),
                                 get_config('TDT_INTERFACE'))
            self._fs = self.tdt.fs

        # scaling factor to ensure uniform intensity across output devices
        self._stim_scaler = _get_stim_scaler(self.audio_controller, stim_amp,
                                             stim_rms)

        # placeholder for extra actions to do on flip-and-play
        self._fp_function = None

        # create visual window
        psylog.info('Expyfun: Setting up screen')
        if window_size is None:
            window_size = get_config('WINDOW_SIZE', '1920,1080').split(',')
        if screen_num is None:
            screen_num = int(get_config('SCREEN_NUM', '0'))
        self.win = visual.Window(size=window_size, fullscr=True, monitor='',
                                 screen=screen_num,
                                 allowGUI=False, allowStencil=False,
                                 color=bkgd_color, colorSpace='rgb')

        # basic components
        self.data_handler = data.ExperimentHandler(name=exp_name, version='',
                                                   extraInfo=self.exp_info,
                                                   runtimeInfo=None,
                                                   originPath=None,
                                                   savePickle=True,
                                                   saveWideText=True,
                                                   dataFileName=basename)
        self.text_stim = visual.TextStim(win=self.win, text='', pos=[0, 0],
                                         height=0.1, wrapWidth=0.8,
                                         units='norm', color=[1, 1, 1],
                                         colorSpace='rgb', opacity=1.0,
                                         contrast=1.0, name='myTextStim',
                                         ori=0, depth=0, flipHoriz=False,
                                         flipVert=False, alignHoriz='center',
                                         alignVert='center', bold=False,
                                         italic=False, font='Arial',
                                         fontFiles=[], antialias=True)
        self.button_handler = event.BuilderKeyResponse()
        #self.shape_stim = visual.ShapeStim()

        # append to list of trial components
        self.trial_components.append(self.button_handler)
        self.trial_components.append(self.text_stim)
        #self.trial_components.append(self.shape_stim)

        psylog.info('Expyfun: Initialization complete')

    def screen_prompt(self, text, max_wait=np.inf, min_wait=0, live_keys=None):
        """Display text and (optionally) wait for user continuation

        Parameters
        ----------
        text : str
            The text to display. It will automatically wrap lines.
        max_wait : float
            The maximum amount of time to wait before returning. Can be np.inf
            to wait until the user responds.
        min_wait : float
            The minimum amount of time to wait before returning. Useful for
            avoiding subjects missing instructions.
        live_keys : list | None
            The acceptable list of buttons to use to advance the trial. If
            None, any button will be accepted.

        Returns
        -------
        val : str | None
            The button that was pressed. Will be None if the function timed
            out before the subject responded.
        time : float
            The timestamp.
        """
        self._show_text(text)
        self.wait_secs(min_wait)
        return self.wait_buttons(live_keys, max_wait=max_wait)

    def _show_text(self, text):
        """Wrapper for PsychoPy's visual.TextStim.SetText() method.

        Parameters
        ----------
        text : str
            The text to be rendered
        """
        self.text_stim.setText(text)
        self.text_stim.tStart = self.t
        self.text_stim.frameNStart = self.f
        self.text_stim.setAutoDraw(True)
        self._flip()

    def clear_screen(self):
        """Remove all visual stimuli from the screen.
        """
        self.text_stim.status = FINISHED
        for comp in self.trial_components:
            if hasattr(comp, 'setAutoDraw'):
                comp.setAutoDraw(False)
        self._flip()

    def init_trial(self):
        """Some housekeeping at the beginning of each trial.
        """
        self.t = 0.0
        self.f = -1
        self.trial_clock.reset()
        self.button_handler.keys = []
        for comp in self.trial_components:
            if hasattr(comp, 'status'):
                comp.status = NOT_STARTED
        self.continue_trial = True

    def end_trial(self):
        """Some housekeeping at the end of each trial.
        """
        self.continue_trial = False
        for comp in self.trial_components:
            if hasattr(comp, 'status') and comp.status != FINISHED:
                self.continue_trial = True
                break  # at least one trial component still running

    def get_buttons(self, live_keys=[]):
        """
        """
        self.t = self.trial_clock.getTime()
        self.f = self.f + 1
        if self.t >= 0.0 and self.button_handler.status == NOT_STARTED:
            self.button_handler.tStart = self.t
            self.button_handler.frameNStart = self.f
            self.button_handler.status = STARTED
            self.button_handler.clock.reset()
            event.clearEvents()
        if self.button_handler.status == STARTED:
            pressed = event.getKeys(live_keys)
            self.button_handler.keys = pressed
            self.button_handler.rt = self.button_handler.clock.getTime()
            return pressed

    def wait_buttons(self, live_keys=None, max_wait=np.inf):
        """XXX add docstring
        """
        return event.waitKeys(maxWait=max_wait, keyList=live_keys,
                              timeStamped=True)

    def save_button_presses(self):
        """Wrapper for PsychoPy's ExperimentHandler methods.
        """
        if len(self.button_handler.keys) == 0:
            self.data_handler.addData('button_presses', None)
        else:
            self.data_handler.addData('reaction_times', self.button_handler.rt)
            self.data_handler.addData('button_presses',
                                      self.button_handler.keys)
        self.data_handler.nextEntry()

    def check_force_quit(self):
        """Wrapper for PsychoPy core.quit()
        """
        if event.getKeys(self._force_quit):
            core.quit()

    def wait_secs(self, secs):
        """Wait a specified number of seconds.

        Parameters
        ----------
        secs : float
            Number of seconds to wait.

        Notes
        -----
        From the PsychoPy documentation:
        If secs=10 and hogCPU=0.2 then for 9.8s python's time.sleep function
        will be used, which is not especially precise, but allows the cpu to
        perform housekeeping. In the final hogCPUperiod the more precise method
        of constantly polling the clock is used for greater precision.

        If you want to obtain key-presses during the wait, be sure to use
        pyglet and to hogCPU for the entire time, and then call
        psychopy.event.getKeys() after calling wait().

        If you want to suppress checking for pyglet events during the wait, do
        this once:
            core.checkPygletDuringWait = False
        and from then on you can do:
            core.wait(sec)
        This will preserve terminal-window focus during command line usage.
        """
        core.wait(secs, hogCPUperiod=secs)

    def load_buffer(self, data, offset=0, buffer_name=None):
        """Load audio data into the audio buffer.

        Parameters
        ----------
        data : np.array
            Audio data as floats scaled to (-1,+1), formatted as an Nx1 or Nx2
            numpy array with dtype float32.
        buffer_name : str | None
            Name of the TDT buffer to target. Ignored if audio_controller is
            'psychopy'.
        """
        psylog.info('Expyfun: Loading {} samples to buffer'.format(data.size))
        if self.audio_controller == 'psychopy':
            self.audio.setSound(np.asarray(data, order='C'))
            self.trial_components.append(self.audio)
        else:
            self.tdt.write_buffer(buffer_name, offset,
                                  data * self._stim_scaler)

    def clear_buffer(self, buffer_name=None):
        """Clear audio data from the audio buffer.

        Parameters
        ----------
        buffer_name : str | None
            Name of the TDT buffer to target. Ignored if audio_controller is
            'psychopy'.
        """
        psylog.info('Expyfun: Clearing buffer')
        if not self.tdt is None:
            self.tdt.clear_buffer(buffer_name)
        else:
            self.audio.setSound(np.zeros((1, 2)))

    def stop_reset(self):
        """Stop audio buffer playback and reset cursor to beginning of buffer.
        """
        psylog.info('Expyfun: Stopping and resetting audio playback')
        self._stop()
        self._reset()

    def close(self):
        """Close all connections in experiment controller.
        """
        self.__exit__(None, None, None)

    def flip_and_play(self):
        """Flip screen and immediately begin playing audio.
        """
        psylog.info('Expyfun: Flipping screen and playing audio')
        self._flip()
        self._play(self.t, self.f)
        if self._fp_function is not None:
            self._fp_function()

    def call_on_flip_and_play(self, function, *args, **kwargs):
        """Locus for additional functions to be executed on flip and play.
        """
        if function is not None:
            self._fp_function = partial(function, *args, **kwargs)
        else:
            self._fp_function = None

    def set_noise_amp(self, new_amp):
        """TODO: add docstring
        """
        self._noise_amp = new_amp

    def set_stim_amp(self, new_amp):
        """TODO: add docstring
        """
        self._stim_amp = new_amp

    def _flip(self):
        """Flip the screen buffer.
        """
        self.win.flip()

    def _play(self, time, frame):
        """Play the audio buffer.
        """
        psylog.debug('Expyfun: playing audio')
        if not self.tdt is None:
            # TODO: detect which triggers are which rather than hard-coding
            self.tdt.trigger(1)
        else:
            self.audio.tStart = time
            self.audio.frameNStart = frame
            self.audio.play()

    def _stop(self):
        """Stop audio buffer playback.
        """
        psylog.debug('Stopping audio')
        if not self.tdt is None:
            # TODO: detect which triggers are which rather than hard-coding
            self.tdt.trigger(2)
        else:
            self.audio.stop()

    def _reset(self):
        """Reset audio buffer to beginning.
        """
        psylog.debug('Expyfun: Resetting audio')
        if not self.tdt is None:
            # TODO: detect which triggers are which rather than hard-coding
            self.tdt.trigger(5)
        else:
            # psychopy defaults to play sounds from beginning
            pass

    def __enter__(self):
        # (for use with "with" syntax) wrap to init? may want to do some
        # low-level stuff to make sure the connection is working?
        psylog.debug('Expyfun: Entering')
        return self

    def __exit__(self, type, value, traceback):
        """
        Notes
        -----
        type, value and traceback will be None when called by self.close()
        """
        # stop the TDT circuit, etc.  (for use with "with" syntax)
        psylog.debug('Expyfun: Exiting cleanly')
        if not self.tdt is None:
            # TODO: detect which triggers are which rather than hard-coding
            self.tdt.trigger(4)  # kill noise
            self.stop_reset()
            self.tdt.halt_circuit()
        core.quit()

    @property
    def fs(self):
        """Playback frequency of the audio controller (samples / second).
        """
        # do it this way so people can't set it
        return self._fs


class TDTObject(object):
    """ TODO: add docstring
    """
    def __init__(self, tdt_type, circuit, interface):
        """Interface for audio output.

        Parameters
        ----------
        tdt_type : str
            String name of the TDT model (e.g., 'RM1', 'RP2', etc).
        circuit : str
            Path to the TDT circuit.
        interface : {'USB','GB'}
            Type of interface between computer and TDT (USB or Gigabit).

        Returns
        -------
        tdt_obj : instance of a TDTObject.
            The object containing all relevant info about the TDT in use.
        """
        self.circuit = circuit
        self.tdt_type = tdt_type
        self.interface = interface
        self.status = None

        # initialize RPcoX connection
        """
        # HIGH-LEVEL APPROACH
        # (fails often, possibly due to inappropriate zBUS call in DSPCircuit)
        import tdt
        self.rpcox = tdt.DSPCircuit(circuit, tdt_type, interface=interface)
        self.rpcox.start()

        # LOW-LEVEL APPROACH (works reliably, but no device abstraction)
        self.rpcox = tdt.actxobjects.RPcoX()
        self.connection = self.rpcox.ConnectRM1(IntName=interface, DevNum=1)
        """
        # MID-LEVEL APPROACH
        if not connect_rpcox is None:
            self.rpcox = connect_rpcox(name=tdt_type, interface=interface,
                                       device_id=1, address=None)
            if not self.rpcox is None:
                psylog.info('Expyfun: RPcoX connection established')
            else:
                raise ExperimentError('Problem initializing RPcoX.')
            """
            # start zBUS (may be needed for devices other than RM1)
            self.zbus = connect_zbus(interface=interface)
            if not self.zbus is None:
                psylog.info('Expyfun: zBUS connection established')
            else:
                raise ExperimentError('Problem initializing zBUS.')
            """
            # load circuit
            if self.rpcox.LoadCOF(circuit):
                psylog.info('Expyfun: TDT circuit loaded')
            else:
                psylog.debug('Expyfun: Problem loading circuit. Clearing...')
                try:
                    if self.rpcox.ClearCOF():
                        psylog.debug('Expyfun: TDT circuit cleared')
                    time.sleep(0.25)
                    if self.rpcox.LoadCOF(circuit):
                        psylog.info('Expyfun: TDT circuit loaded')
                except:
                    raise ExperimentError('Expyfun: Problem loading circuit.')
            psylog.info('Expyfun: Circuit {0} loaded to {1} via '
                        '{2}.'.format(circuit, tdt_type, interface))
            # run circuit
            if self.rpcox.Run():
                psylog.info('Expyfun: TDT circuit running')
            else:
                raise ExperimentError('Expyfun: Problem starting TDT circuit.')
            time.sleep(0.25)

    @property
    def fs(self):
        """Playback frequency of the audio (samples / second).

        Notes
        -----
        When using PsychoPy for audio, fs is potentially user-settable, but
        defaults to 22050 Hz.  When using TDT for audio, fs is read from the
        TDT circuit.
        """
        return np.float(self.rpcox.GetSFreq())

    def trigger(self, trigger_number):
        """Wrapper for tdt.util.RPcoX.SoftTrg()

        Parameters
        ----------
        trigger_number : int
            Trigger number to send to TDT.

        Returns
        -------
        trigger_sent : {0,1}
            Boolean integer indicating success or failure of buffer clear.
        """
        self.rpcox.SoftTrg(trigger_number)

    def write_buffer(self, data, offset, buffer_name):
        """Wrapper for tdt.util.RPcoX.WriteTagV()
        """
        # TODO: check to make sure data is properly formatted / correct dtype
        # check dimensions of array
        # cast as np.float32 with order='C'
        self.rpcox.WriteTagV(buffer_name, offset, data)

    def clear_buffer(self, buffer_name):
        """Wrapper for tdt.util.RPcoX.ZeroTag()
        """
        self.rpcox.ZeroTag(buffer_name)

    def halt_circuit(self):
        """Wrapper for tdt.util.RPcoX.Halt()
        """
        self.rpcox.Halt()


def _get_stim_scaler(audio_controller, stim_amp, stim_rms):
    exponent = (-(_get_tdt_rms(audio_controller) - stim_amp) / 20) / stim_rms
    return np.power(10, exponent)


def _get_tdt_rms(tdt_type):
    if tdt_type is 'RM1':
        return 108  # this is approx; knob is not detented
    elif tdt_type is 'RP2':
        return 108
    elif tdt_type is 'RZ6':
        return 114
    else:
        return 90  # for untested models or internal sound cards


def get_tdt_rates():
    return {'6k': 6103.515625, '12k': 12207.03125, '25k': 24414.0625,
            '50k': 48828.125, '100k': 97656.25, '200k': 195312.5}


class ExperimentError(Exception):
    """
    Exceptions unique to the ExperimentController class and its derivatives.

    Attributes:
        msg -- explanation of the error.
    """

    def __init__(self, msg):
        self.msg = msg
