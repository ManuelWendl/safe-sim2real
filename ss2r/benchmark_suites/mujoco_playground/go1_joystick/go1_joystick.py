import jax
import jax.numpy as jnp
from brax.envs import Env, State, Wrapper
from mujoco_playground import locomotion

FLOOR_GEOM_ID = 0
TORSO_BODY_ID = 1


def domain_randomization(sys, rng, cfg):
    @jax.vmap
    def rand_dynamics(rng):
        model = sys
        # Floor friction: =U(0.4, 1.0).
        rng, key = jax.random.split(rng)
        geom_friction_sample = jax.random.uniform(
            key, minval=cfg.floor_friction[0], maxval=cfg.floor_friction[1]
        )
        geom_friction = model.geom_friction.at[FLOOR_GEOM_ID, 0].set(
            geom_friction_sample
        )

        # Scale static friction: *U(0.9, 1.1).
        rng, key = jax.random.split(rng)
        friction_loss_sample = jax.random.uniform(
            key, shape=(12,), minval=cfg.scale_friction[0], maxval=cfg.scale_friction[1]
        )
        frictionloss = model.dof_frictionloss[6:] * friction_loss_sample
        dof_frictionloss = model.dof_frictionloss.at[6:].set(frictionloss)
        # Scale armature: *U(1.0, 1.05).
        rng, key = jax.random.split(rng)
        armature_sample = jax.random.uniform(
            key, shape=(12,), minval=cfg.scale_armature[0], maxval=cfg.scale_armature[1]
        )

        armature = model.dof_armature[6:] * armature_sample
        dof_armature = model.dof_armature.at[6:].set(armature)

        # Jitter center of mass positiion: +U(-0.05, 0.05).
        rng, key = jax.random.split(rng)
        dpos = jax.random.uniform(
            key, (3,), minval=cfg.jitter_mass[0], maxval=cfg.jitter_mass[1]
        )
        body_ipos = model.body_ipos.at[TORSO_BODY_ID].set(
            model.body_ipos[TORSO_BODY_ID] + dpos
        )

        # Scale all link masses: *U(0.9, 1.1).
        rng, key = jax.random.split(rng)
        dmass = jax.random.uniform(
            key,
            shape=(model.nbody,),
            minval=cfg.scale_link_mass[0],
            maxval=cfg.scale_link_mass[1],
        )
        body_mass = model.body_mass.at[:].set(model.body_mass * dmass)

        # Add mass to torso: +U(-1.0, 1.0).
        rng, key = jax.random.split(rng)
        dmass_torso = jax.random.uniform(
            key, minval=cfg.add_torso_mass[0], maxval=cfg.add_torso_mass[1]
        )
        body_mass = body_mass.at[TORSO_BODY_ID].set(
            body_mass[TORSO_BODY_ID] + dmass_torso
        )

        # Jitter qpos0: +U(-0.05, 0.05).
        rng, key = jax.random.split(rng)
        qpos0 = model.qpos0
        qpos0 = qpos0.at[7:].set(
            qpos0[7:]
            + jax.random.uniform(
                key, shape=(12,), minval=cfg.jitter_qpos0[0], maxval=cfg.jitter_qpos0[1]
            )
        )
        kd = jax.random.uniform(key, shape=(12,), minval=cfg.Kd[0], maxval=cfg.Kd[1])
        dof_damping = model.dof_damping.at[6:].add(kd)
        kp = jax.random.uniform(key, shape=(12,), minval=cfg.Kp[0], maxval=cfg.Kp[1])
        actuator_gainprm = model.actuator_gainprm.at[:, 0].add(kp)
        actuator_biasprm = model.actuator_biasprm.at[:, 1].add(-kp)
        samples = jnp.hstack(
            [
                geom_friction_sample,
                friction_loss_sample,
                armature_sample,
                dpos,
                dmass,
                dmass_torso,
                kd,
                kp,
            ]
        )
        return (
            geom_friction,
            body_ipos,
            body_mass,
            qpos0,
            dof_frictionloss,
            dof_armature,
            dof_damping,
            actuator_gainprm,
            actuator_biasprm,
            samples,
        )

    (
        friction,
        body_ipos,
        body_mass,
        qpos0,
        dof_frictionloss,
        dof_armature,
        dof_damping,
        actuator_gainprm,
        actuator_biasprm,
        samples,
    ) = rand_dynamics(rng)

    in_axes = jax.tree_util.tree_map(lambda x: None, sys)
    in_axes = in_axes.tree_replace(
        {
            "geom_friction": 0,
            "body_ipos": 0,
            "body_mass": 0,
            "qpos0": 0,
            "dof_frictionloss": 0,
            "dof_armature": 0,
            "dof_damping": 0,
            "actuator_gainprm": 0,
            "actuator_biasprm": 0,
        }
    )

    model = sys.tree_replace(
        {
            "geom_friction": friction,
            "body_ipos": body_ipos,
            "body_mass": body_mass,
            "qpos0": qpos0,
            "dof_frictionloss": dof_frictionloss,
            "dof_armature": dof_armature,
            "dof_damping": dof_damping,
            "actuator_gainprm": actuator_gainprm,
            "actuator_biasprm": actuator_biasprm,
        }
    )
    return model, in_axes, samples


class JointConstraintWrapper(Wrapper):
    def __init__(self, env: Env):
        super().__init__(env)
        self.env._config.reward_config.scales["dof_pos_limits"] = 0.0

    def reset(self, rng: jax.Array) -> State:
        state = self.env.reset(rng)
        joint_cost = self._cost_joint_pos_limits(state.data.qpos[7:])
        state.info["cost"] = jnp.where(joint_cost > 0.0, 1.0, 0.0)
        return state

    def step(self, state: State, action: jax.Array) -> State:
        state = self.env.step(state, action)
        joint_cost = self._cost_joint_pos_limits(state.data.qpos[7:])
        state = state.replace(info={**state.info, "cost": joint_cost})
        return state


class JointTorqueConstraintWrapper(Wrapper):
    def __init__(self, env: Env, limit: float):
        super().__init__(env)
        self.env._config.reward_config.scales["torques"] = 0.0
        self.limit = limit

    def reset(self, rng: jax.Array) -> State:
        state = self.env.reset(rng)
        state.info["cost"] = jnp.zeros_like(state.reward)
        return state

    def step(self, state: State, action: jax.Array) -> State:
        state = self.env.step(state, action)
        torques = state.data.actuator_force
        cost = jnp.clip((jnp.abs(torques) - self.limit), a_min=0.0).max()
        state = state.replace(info={**state.info, "cost": cost})
        return state


class FlipConstraintWrapper(Wrapper):
    def __init__(self, env: Env):
        super().__init__(env)

    def reset(self, rng):
        state = self.env.reset(rng)
        state.info["cost"] = jnp.zeros_like(state.reward)
        state.metrics["cost/slip"] = jnp.zeros_like(state.reward)
        state.metrics["cost/orientation"] = jnp.zeros_like(state.reward)
        return state

    def step(self, state, action):
        nstate = self.env.step(state, action)
        xy = self.env.get_upvector(nstate.data)[:2]
        # Put more cost on rolling
        weights = jnp.array([0.4, 0.6])
        orientation_cost = jnp.sum(jnp.square(xy) * weights)
        contact = nstate.info["last_contact"]
        # Use the previous state info
        slippage_cost = self.env._cost_feet_slip(nstate.data, contact, state.info)
        cost = slippage_cost * (1.0 + orientation_cost) * 0.5 + orientation_cost
        nstate.metrics["cost/slip"] = slippage_cost
        nstate.metrics["cost/orientation"] = orientation_cost
        nstate.info["cost"] = cost
        return nstate


name = "Go1JoystickFlatTerrain"


def make_joint(**kwargs):
    env = locomotion.load(name, **kwargs)
    env = JointConstraintWrapper(env)
    return env


def make_joint_torque(**kwargs):
    limit = kwargs["config"]["torque_limit"]
    env = locomotion.load(name, **kwargs)
    env = JointTorqueConstraintWrapper(env, limit)
    return env


def make_flip(**kwargs):
    env = locomotion.load(name, **kwargs)
    env = FlipConstraintWrapper(env)
    return env


locomotion.register_environment(
    f"SafeJoint{name}", make_joint, locomotion.go1_joystick.default_config
)
locomotion.register_environment(
    f"SafeJointTorque{name}", make_joint_torque, locomotion.go1_joystick.default_config
)
locomotion.register_environment(
    f"SafeFlip{name}", make_flip, locomotion.go1_joystick.default_config
)
