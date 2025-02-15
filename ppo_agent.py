import numpy as np
import torch
from torch import optim
from utils import select_actions, evaluate_actions, config_logger
from datetime import datetime
import os
import copy

class ppo_agent:
    def __init__(self, envs, args, net, env_net=None):
        self.envs = envs 
        self.args = args
        # define the newtork...
        self.net = net
        self.old_net = copy.deepcopy(self.net)
        # if use the cuda...
        if self.args.cuda:
            self.net.cuda()
            self.old_net.cuda()
        # define the optimizer...
        self.optimizer = optim.Adam(self.net.parameters(), self.args.lr, eps=self.args.eps)
        # check saving folder..
        if not os.path.exists(self.args.save_dir):
            os.mkdir(self.args.save_dir)
        # env folder..
        self.model_path = os.path.join(self.args.save_dir, self.args.env_name)
        if not os.path.exists(self.model_path):
            os.mkdir(self.model_path)
        # logger folder
        if not os.path.exists(self.args.log_dir):
            os.mkdir(self.args.log_dir)
        self.log_path = self.args.log_dir + self.args.env_name + '.log'
        # get the observation
        self.batch_ob_shape = (self.args.num_workers * self.args.nsteps, ) + self.envs.observation_space.shape
        self.obs = np.zeros((self.args.num_workers, ) + self.envs.observation_space.shape, dtype=self.envs.observation_space.dtype.name)
        self.obs[:] = self.envs.reset()
        self.dones = [False for _ in range(self.args.num_workers)]
        self.logger = config_logger(self.log_path)
        self.env_net = env_net
        self.policy_optimizer = optim.Adam(self.net.parameters(), lr=1e-5)
        self.env_optimizer = optim.Adam(self.env_net.parameters(), lr=1e-5)
        self.obs_shape = envs.observation_space.shape

    # start to train the network...
    def learn(self):
        num_updates = self.args.total_frames // (self.args.nsteps * self.args.num_workers)
        # get the reward to calculate other informations
        episode_rewards = torch.zeros([self.args.num_workers, 1])
        final_rewards = torch.zeros([self.args.num_workers, 1])
        reward_hist = []
        policy_loss_hist = []
        env_loss_hist = []
        for update in range(num_updates):
            mb_obs, mb_rewards, mb_actions, mb_dones, mb_values = [], [], [], [], []
            if self.args.lr_decay:
                self._adjust_learning_rate(update, num_updates)
            for step in range(self.args.nsteps):
                with torch.no_grad():
                    # get tensors
                    obs_tensor = self._get_tensors(self.obs)
                    values, pis = self.net(obs_tensor)
                # select actions
                actions = select_actions(pis)
                # get the input actions
                input_actions = actions 
                # start to store information
                mb_obs.append(np.copy(self.obs))
                mb_actions.append(actions)
                mb_dones.append(self.dones)
                mb_values.append(values.detach().cpu().numpy().squeeze())
                # start to excute the actions in the environment
                obs, rewards, dones, _ = self.envs.step(input_actions)
                # update dones
                self.dones = dones
                mb_rewards.append(rewards)
                # clear the observation
                for n, done in enumerate(dones):
                    if done:
                        self.obs[n] = self.obs[n] * 0
                self.obs = obs
                # process the rewards part -- display the rewards on the screen
                rewards = torch.tensor(np.expand_dims(np.stack(rewards), 1), dtype=torch.float32)
                episode_rewards += rewards
                masks = torch.tensor([[0.0] if done_ else [1.0] for done_ in dones], dtype=torch.float32)
                final_rewards *= masks
                final_rewards += (1 - masks) * episode_rewards
                episode_rewards *= masks
            # process the rollouts
            mb_obs = np.asarray(mb_obs, dtype=np.float32)
            mb_rewards = np.asarray(mb_rewards, dtype=np.float32)
            mb_actions = np.asarray(mb_actions, dtype=np.float32)
            mb_dones = np.asarray(mb_dones, dtype=np.bool)
            mb_values = np.asarray(mb_values, dtype=np.float32)
            # compute the last state value
            with torch.no_grad():
                obs_tensor = self._get_tensors(self.obs)
                last_values, _ = self.net(obs_tensor)
                last_values = last_values.detach().cpu().numpy().squeeze()
            # start to compute advantages...
            mb_returns = np.zeros_like(mb_rewards)
            mb_advs = np.zeros_like(mb_rewards)
            lastgaelam = 0
            for t in reversed(range(self.args.nsteps)):
                if t == self.args.nsteps - 1:
                    nextnonterminal = 1.0 - self.dones
                    nextvalues = last_values
                else:
                    nextnonterminal = 1.0 - mb_dones[t + 1]
                    nextvalues = mb_values[t + 1]
                delta = mb_rewards[t] + self.args.gamma * nextvalues * nextnonterminal - mb_values[t]
                mb_advs[t] = lastgaelam = delta + self.args.gamma * self.args.tau * nextnonterminal * lastgaelam
            mb_returns = mb_advs + mb_values
            # after compute the returns, let's process the rollouts
            mb_obs = mb_obs.swapaxes(0, 1).reshape(self.batch_ob_shape)
            mb_actions = mb_actions.swapaxes(0, 1).flatten()
            mb_returns = mb_returns.swapaxes(0, 1).flatten()
            mb_advs = mb_advs.swapaxes(0, 1).flatten()
            # before update the network, the old network will try to load the weights
            self.old_net.load_state_dict(self.net.state_dict())
            # start to update the network
            pl, vl, ent = self._update_network(mb_obs, mb_actions, mb_returns, mb_advs)
            # env_loss, policy_loss = self._update_network_by_env_net(mb_obs, mb_actions, mb_rewards)
            # display the training information
            reward_hist.append(final_rewards.mean().detach().cpu().numpy())
            policy_loss_hist.append(pl)
            # env_loss_hist.append(env_loss)
            if update % self.args.display_interval == 0:
                self.logger.info('[{}] Update: {} / {}, Frames: {}, Rewards: {:.3f}, Min: {:.3f}, Max: {:.3f}'
                                 .format(datetime.now(), update, num_updates, (update + 1)*self.args.nsteps*self.args.num_workers, \
                                final_rewards.mean().item(), final_rewards.min().item(), final_rewards.max().item()))
                # save the model
                torch.save(self.net.state_dict(), self.model_path + '/model.pt')
        return reward_hist, env_loss_hist, policy_loss_hist



    # convert the numpy array to tensors
    def _get_tensors(self, obs):
        obs_tensor = torch.tensor(np.transpose(obs, (0, 3, 1, 2)), dtype=torch.float32)
        # decide if put the tensor on the GPU
        if self.args.cuda:
            obs_tensor = obs_tensor.cuda()
        return obs_tensor

    # adjust the learning rate
    def _adjust_learning_rate(self, update, num_updates):
        lr_frac = 1 - (update / num_updates)
        adjust_lr = self.args.lr * lr_frac
        for param_group in self.policy_optimizer.param_groups:
             param_group['lr'] = adjust_lr

    def _update_network_by_env_net(self, obs, actions, reward):
        lens_data = np.minimum(np.minimum(len(obs), len(actions)),len(reward))
        inds = np.arange(lens_data)
        nbatch_train = lens_data // self.args.batch_size
        BCE_loss = torch.nn.BCELoss()
        L1_loss = torch.nn.L1Loss()

        for _ in range(self.args.epoch):
            np.random.shuffle(inds)
            for start in range(0, lens_data, nbatch_train):
                # get the mini-batchs
                end = start + nbatch_train
                binds = inds[start:end]
                obs_temp = obs[binds]
                actions_temp = actions[binds]
                reward_temp = reward[binds]
                obs_temp = torch.tensor(obs_temp, dtype=torch.float32)
                actions_temp = torch.tensor(actions_temp, dtype=torch.float32)
                reward_temp = torch.tensor(reward_temp, dtype=torch.float32)

                self.env_optimizer.zero_grad()
                obs_env = torch.reshape(obs_temp, [-1, 110592])
                actions_env = torch.reshape(actions_temp, [-1, 1])
                pred_reward = self.env_net(obs_env, actions_env)
                env_loss = BCE_loss(pred_reward.reshape(-1,1), reward_temp.reshape(-1, 1))
                torch.nn.utils.clip_grad_norm_(self.env_net.parameters(), self.args.max_grad_norm)
                env_loss.backward()
                self.env_optimizer.step()
                # if np.mean(reward) > 0:

                obs_shape = (-1, ) + self.obs_shape
                obs_temp2 = np.reshape(obs_temp.detach().cpu().numpy(), obs_shape)
                obs_temp2 = np.transpose(obs_temp2, (0, 3, 1, 2))
                obs_temp2 = torch.tensor(obs_temp2, dtype=torch.float32)

                _, pis = self.net(obs_temp2)
                try:
                    pred_action = select_actions(pis)
                except:
                    pis = torch.tensor(np.ones_like(pis.detach().cpu().numpy())/2)
                    pred_action = select_actions(pis)
                pred_action = np.reshape(pred_action, [-1, 1])
                pred_action = torch.tensor(pred_action, dtype=torch.float32)
                obs_env = torch.reshape(obs_temp, [-1, 110592])
                actions_env = torch.reshape(pred_action, [-1, 1])
                corresponding_reward = self.env_net(obs_env, actions_env)
                policy_loss = BCE_loss(corresponding_reward, torch.ones_like(corresponding_reward)/2)
                torch.nn.utils.clip_grad_norm_(self.net.parameters(), self.args.max_grad_norm)
                self.policy_optimizer.zero_grad()
                policy_loss.backward()
                self.policy_optimizer.step()
                if not os.path.exists('policy_model'):
                    os.makedirs('policy_model')
                torch.save(self.net.state_dict(), self.args.policy_model_dir + '/policy_net.pth')
        return env_loss.item(), policy_loss.item()


        # update the network
    def _update_network(self, obs, actions, returns, advantages):
        inds = np.arange(obs.shape[0])
        nbatch_train = obs.shape[0] // self.args.batch_size

        for _ in range(self.args.epoch):
            np.random.shuffle(inds)
            for start in range(0, obs.shape[0], nbatch_train):
                # get the mini-batchs
                end = start + nbatch_train
                mbinds = inds[start:end]
                mb_obs = obs[mbinds]
                mb_actions = actions[mbinds]
                mb_returns = returns[mbinds]
                mb_advs = advantages[mbinds]
                # convert minibatches to tensor
                mb_obs = self._get_tensors(mb_obs)
                mb_actions = torch.tensor(mb_actions, dtype=torch.float32)
                mb_returns = torch.tensor(mb_returns, dtype=torch.float32).unsqueeze(1)
                mb_advs = torch.tensor(mb_advs, dtype=torch.float32).unsqueeze(1)
                # normalize adv
                mb_advs = (mb_advs - mb_advs.mean()) / (mb_advs.std() + 1e-8)
                if self.args.cuda:
                    mb_actions = mb_actions.cuda()
                    mb_returns = mb_returns.cuda()
                    mb_advs = mb_advs.cuda()
                # start to get values
                mb_values, pis = self.net(mb_obs)
                # start to calculate the value loss...
                value_loss = (mb_returns - mb_values).pow(2).mean()
                # start to calculate the policy loss
                with torch.no_grad():
                    _, old_pis = self.old_net(mb_obs)
                    # get the old log probs
                    old_log_prob, _ = evaluate_actions(old_pis, mb_actions)
                    old_log_prob = old_log_prob.detach()
                # evaluate the current policy
                log_prob, ent_loss = evaluate_actions(pis, mb_actions)
                prob_ratio = torch.exp(log_prob - old_log_prob)
                # surr1
                surr1 = prob_ratio * mb_advs
                surr2 = torch.clamp(prob_ratio, 1 - self.args.clip, 1 + self.args.clip) * mb_advs
                policy_loss = -torch.min(surr1, surr2).mean()
                # final total loss
                total_loss = policy_loss + self.args.vloss_coef * value_loss - ent_loss * self.args.ent_coef
                # clear the grad buffer
                self.optimizer.zero_grad()
                total_loss.backward()
                torch.nn.utils.clip_grad_norm_(self.net.parameters(), self.args.max_grad_norm)
                # update
                self.optimizer.step()
        return policy_loss.item(), value_loss.item(), ent_loss.item()