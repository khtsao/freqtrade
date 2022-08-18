import logging
from typing import Any, Dict  # Optional
import torch as th
import numpy as np
import gym
from typing import Callable
from stable_baselines3.common.callbacks import EvalCallback
# EvalCallback , StopTrainingOnNoModelImprovement, StopTrainingOnRewardThreshold
from stable_baselines3.common.monitor import Monitor
from stable_baselines3.common.vec_env import SubprocVecEnv
from stable_baselines3.common.utils import set_random_seed
from stable_baselines3 import DQN
from freqtrade.freqai.RL.Base5ActionRLEnv import Base5ActionRLEnv, Actions, Positions
from freqtrade.freqai.RL.BaseReinforcementLearningModel import BaseReinforcementLearningModel
from freqtrade.freqai.RL.TDQNagent import TDQN
from stable_baselines3.common.buffers import ReplayBuffer
from freqtrade.freqai.data_kitchen import FreqaiDataKitchen

logger = logging.getLogger(__name__)


def make_env(env_id: str, rank: int, seed: int, train_df, price,
             reward_params, window_size, monitor=False) -> Callable:
    """
    Utility function for multiprocessed env.

    :param env_id: (str) the environment ID
    :param num_env: (int) the number of environment you wish to have in subprocesses
    :param seed: (int) the inital seed for RNG
    :param rank: (int) index of the subprocess
    :return: (Callable)
    """
    def _init() -> gym.Env:

        env = MyRLEnv(df=train_df, prices=price, window_size=window_size,
                      reward_kwargs=reward_params, id=env_id, seed=seed + rank)
        if monitor:
            env = Monitor(env, ".")
        return env
    set_random_seed(seed)
    return _init


class ReinforcementLearningTDQN_multiproc(BaseReinforcementLearningModel):
    """
    User created Reinforcement Learning Model prediction model.
    """

    def fit_rl(self, data_dictionary: Dict[str, Any], dk: FreqaiDataKitchen):

        train_df = data_dictionary["train_features"]
        test_df = data_dictionary["test_features"]
        eval_freq = self.freqai_info["rl_config"]["eval_cycles"] * len(test_df)
        total_timesteps = self.freqai_info["rl_config"]["train_cycles"] * len(train_df)

        path = dk.data_path

        eval_callback = EvalCallback(self.eval_env, best_model_save_path=f"{path}/",
                                     log_path=f"{path}/tdqn/logs/", eval_freq=int(eval_freq),
                                     deterministic=True, render=False)
        # model arch
        policy_kwargs = dict(activation_fn=th.nn.ReLU,
                             net_arch=[512, 512, 512])

        model = TDQN('TMultiInputPolicy', self.train_env,
                     policy_kwargs=policy_kwargs,
                     tensorboard_log=f"{path}/tdqn/tensorboard/",
                     replay_buffer_class=ReplayBuffer,
                     **self.freqai_info['model_training_parameters']
                     )

        model.learn(
            total_timesteps=int(total_timesteps),
            callback=eval_callback
        )

        best_model = DQN.load(dk.data_path / "best_model.zip")
        print('Training finished!')

        return best_model

    def set_train_and_eval_environments(self, data_dictionary, prices_train, prices_test):
        """
        User overrides this in their prediction model if they are custom a MyRLEnv. Othwerwise
        leaving this will default to Base5ActEnv
        """
        train_df = data_dictionary["train_features"]
        test_df = data_dictionary["test_features"]

        # environments
        if not self.train_env:
            env_id = "train_env"
            num_cpu = int(self.freqai_info["data_kitchen_thread_count"] / 2)
            self.train_env = SubprocVecEnv([make_env(env_id, i, 1, train_df, prices_train,
                                            self.reward_params, self.CONV_WIDTH) for i
                                            in range(num_cpu)])

            eval_env_id = 'eval_env'
            self.eval_env = SubprocVecEnv([make_env(eval_env_id, i, 1, test_df, prices_test,
                                           self.reward_params, self.CONV_WIDTH, monitor=True) for i
                                           in range(num_cpu)])
        else:
            self.train_env.env_method('reset_env', train_df, prices_train,
                                      self.CONV_WIDTH, self.reward_params)
            self.eval_env.env_method('reset_env', train_df, prices_train,
                                     self.CONV_WIDTH, self.reward_params)
            self.train_env.env_method('reset')
            self.eval_env.env_method('reset')

# User can inherit and customize 5 action environment


class MyRLEnv(Base5ActionRLEnv):
    """
    User can override any function in BaseRLEnv and gym.Env. Here the user
    Adds 5 actions.
    """

    def calculate_reward(self, action):

        if self._last_trade_tick is None:
            return 0.

        # close long
        if action == Actions.Long_sell.value and self._position == Positions.Long:
            last_trade_price = self.add_buy_fee(self.prices.iloc[self._last_trade_tick].open)
            current_price = self.add_sell_fee(self.prices.iloc[self._current_tick].open)
            return float(np.log(current_price) - np.log(last_trade_price))

        if action == Actions.Long_sell.value and self._position == Positions.Long:
            if self.close_trade_profit[-1] > self.profit_aim * self.rr:
                last_trade_price = self.add_buy_fee(self.prices.iloc[self._last_trade_tick].open)
                current_price = self.add_sell_fee(self.prices.iloc[self._current_tick].open)
                return float((np.log(current_price) - np.log(last_trade_price)) * 2)

        # close short
        if action == Actions.Short_buy.value and self._position == Positions.Short:
            last_trade_price = self.add_sell_fee(self.prices.iloc[self._last_trade_tick].open)
            current_price = self.add_buy_fee(self.prices.iloc[self._current_tick].open)
            return float(np.log(last_trade_price) - np.log(current_price))

        if action == Actions.Short_buy.value and self._position == Positions.Short:
            if self.close_trade_profit[-1] > self.profit_aim * self.rr:
                last_trade_price = self.add_sell_fee(self.prices.iloc[self._last_trade_tick].open)
                current_price = self.add_buy_fee(self.prices.iloc[self._current_tick].open)
                return float((np.log(last_trade_price) - np.log(current_price)) * 2)

        return 0.