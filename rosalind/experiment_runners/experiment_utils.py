import logging
import uuid
import os
import shutil
import time
import traceback
import datetime

from multiprocessing import Process
from experiments.a2c.train import train_a2c
from experiments.deep_dqn.train import train_dqn

from rosalind.experiment_runners.experiment_monitor import ExperimentMonitor
from rosalind.db.connection import RosalindDatabase
from rosalind.db.schema import Experiments, ClientPool
from rosalind.db.queries import create_experiment, update_experiment, \
    find_and_reserve_client, update_client, get_user, get_experiment,get_experiments_by_group_id
from rosalind.db.types import ClientStatus, ExperimentStatus

_MODELS = {
    'a2c': train_a2c,
    'deepq': train_dqn
}


def build_log_dir(experiment_id):
    base_log_dir = os.path.join(os.path.dirname(__file__), "logs")

    if not os.path.isdir(base_log_dir):
        os.mkdir(base_log_dir)

    log_dir = os.path.join(base_log_dir, str(experiment_id))

    if not os.path.isdir(log_dir):
        os.mkdir(log_dir)

    return log_dir


def copy_files(src, dst, symlinks=False, ignore=None):
    for item in os.listdir(src):
        s = os.path.join(src, item)
        d = os.path.join(dst, item)
        if not os.path.isdir(s):
            shutil.copy2(s, d)

def save_experiment_checkpoint(log_dir:str):
    """
    This function saves a checkpoint of all an experiments log and model definition files, in case of some event
    that causes a new running experiment to fail.

    :param log_dir:
    :return:
    """
    checkpoint_dir = os.path.join(log_dir, "checkpoint")

    if not os.path.isdir(checkpoint_dir):
        os.mkdir(checkpoint_dir)

    copy_files(log_dir, checkpoint_dir)


def send_message_to_owner(bot, experiment, message):
    """
    This function sends a telegram message to the experiment owner.

    :param experiment:
    :param message:
    :return:
    """

    user = get_user(rosalind_connection=bot.db, user_id=experiment.owner)

    bot.send_message(chat_id=user.chat_id,
                     text=message)


def train_model(
        bot,
        experiment:Experiments,
        log_dir: str,
        model_runner,
        env_id: str,
        model_params,
        num_envs=1):
    logger = logging.getLogger("experiment-{}".format(experiment.id))
    fh = logging.FileHandler(os.path.join(log_dir, 'train.log'))
    fh.setLevel(logging.INFO)
    logger.addHandler(fh)

    # connections are not process safe, so we need a new one per process.
    bot.db = RosalindDatabase()

    experiment_monitor = ExperimentMonitor(log_dir=log_dir,
                                           name="ExperimentMonitor-{}".format(experiment.id),
                                           experiment_id=experiment.id,
                                           bot=bot, logger=logger)

    client_pool=[]

    for i in range(num_envs):

        client_address = None

        while not client_address:
            client_address = find_and_reserve_client(rosalind_connection=bot.db, experiment_id=experiment.id)
            logger.info("No clients available, waiting for available clients...")
            time.sleep(10)

        logger.debug("Reserving Client {}".format(client_address))

        client = client_address.split(":")
        client = (client_pool[0], int(client_pool[1]))
        client_pool.append(client)


    try:

        update_experiment(rosalind_connection=bot.db,
                          experiment_id=experiment.id,
                          fields={Experiments.status: ExperimentStatus.RUNNING.name})

        experiment_monitor.start()

        model_runner(log_dir,
                     env_id,
                     client_pool,
                     10,
                     logger,
                     **model_params)

    except Exception as e:

        experiment_monitor.stop()
        experiment_monitor.join(timeout=2)

        update_experiment(rosalind_connection=bot.db,
                          experiment_id=experiment.id,
                          fields={Experiments.status: ExperimentStatus.FAILED.name,
                                  Experiments.end_date: datetime.datetime.utcnow()})

        logger.exception("Experiment {} Failed!".format(experiment.id))
        tb = traceback.format_exc()
        send_message_to_owner(bot=bot,
                              experiment=experiment,
                              message='The experiment with id {} failed!'
                                      'Traceback: \n '
                                      '{}'.format(experiment.id, tb))
        raise e
    else:

        update_experiment(rosalind_connection=bot.db,
                          experiment_id=experiment.id,
                          fields={Experiments.status: ExperimentStatus.COMPLETED.name,
                                  Experiments.end_date: datetime.datetime.utcnow()})
        send_message_to_owner(bot=bot,
                              experiment=experiment,
                              message='The experiment with id {} completed!'.format(experiment.id))
    finally:
        experiment_monitor.stop()
        experiment_monitor.join(timeout=2)
        update_client(rosalind_connection=bot.db,
                      client_address=client_address,
                      fields={ClientPool.status: ClientStatus.AVALIABLE.name,
                              ClientPool.current_experiment: None})


def run_new_single_experiment_with_monitoring(bot,
                                              user,
                                              model: str,
                                              env_id: str,
                                              model_params,
                                              group_id=None):
    model_runner = _MODELS.get(model)

    if not model_runner:
        raise KeyError("The model {} has not been implemented.".format(model))

    experiment_id = uuid.uuid4()

    if not group_id:
        group_id = uuid.uuid4()

    log_dir = build_log_dir(experiment_id)

    create_experiment(rosalind_connection=bot.db,
                      experiment_id=experiment_id,
                      group_id=group_id,
                      model=model,
                      env_id=env_id,
                      owner=user,
                      total_timesteps=model_params['total_timesteps'],
                      log_dir=log_dir,
                      model_params=model_params)
    experiment = get_experiment(rosalind_connection=bot.db, experiment_id=experiment_id)

    try:
        training_process = Process(target=train_model, args=(bot,
                                                             experiment,
                                                             log_dir,
                                                             model_runner,
                                                             env_id,
                                                             model_params))
        training_process.start()
    except Exception as e:
        tb = traceback.format_exc()
        send_message_to_owner(bot=bot,
                              experiment=experiment,
                              message='The experiment with id {} failed!'
                                      'Traceback: \n '
                                      '{}'.format(experiment.id, tb))
        raise e

    update_experiment(rosalind_connection=bot.db,
                      experiment_id=experiment.id,
                      fields={Experiments.pid: training_process.pid})

    return experiment

def continue_experiement_with_monitoring(bot,
                                         user,
                                         experiment:Experiments,
                                         total_timesteps:int=500000):
    """
    This function picks up a previously completed experiment and continues training it,
    At start it creates a checkpoint, such that the original experiment results can be recovered.

    :param bot:
    :param user:
    :param experiment:
    :param timesteps:
    :return:
    """

    model_runner = _MODELS.get(experiment.model)

    if not model_runner:
        raise KeyError("The model {} has not been implemented.".format(experiment.model))

    model_params = experiment.model_params

    log_dir = build_log_dir(experiment.id)

    save_experiment_checkpoint(log_dir)

    model_params["load_path"] = os.path.join(log_dir, 'model.pkl')
    model_params["total_timesteps"] = total_timesteps

    update_experiment(rosalind_connection=bot.db,
                      experiment_id=experiment.id,
                      fields={Experiments.total_timesteps: experiment.total_timesteps + total_timesteps,
                              Experiments.status: ExperimentStatus.PENDING.name,
                              Experiments.model_params: model_params,
                              Experiments.owner: user.id})

    try:
        training_process = Process(target=train_model, args=(bot,
                                                             experiment,
                                                             log_dir,
                                                             model_runner,
                                                             experiment.env_id,
                                                             model_params))
        training_process.start()
    except Exception as e:
        tb = traceback.format_exc()
        send_message_to_owner(bot=bot,
                              experiment=experiment,
                              message='The experiment with id {} failed!'
                                      'Traceback: \n '
                                      '{}'.format(experiment.id, tb))
        raise e

    update_experiment(rosalind_connection=bot.db,
                      experiment_id=experiment.id,
                      fields={Experiments.pid: training_process.pid})

    return experiment

def continue_experiment_group(bot,
                              user,
                              group_id:str,
                              total_timesteps:int=500000):
    """
    This function restarts all experiments within a group and extends them for additional  timesteps.

    :param bot:
    :param user:
    :param group_id:
    :param total_timesteps:
    :return:
    """

    experiments = get_experiments_by_group_id(rosalind_connection=bot.db, group_id=group_id)

    for experiment in experiments:
        continue_experiement_with_monitoring(bot,
                                             user,
                                             experiment,
                                             total_timesteps)
