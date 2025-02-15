from functools import partial

import diffrax as dx 
import einops  
import equinox as eqx
import jax
import jax.numpy as jnp
import matplotlib.pyplot as plt
import optax  

from utils import load_mnist, dataloader


class MixerBlock(eqx.Module):
    patch_mixer: eqx.nn.MLP
    hidden_mixer: eqx.nn.MLP
    norm1: eqx.nn.LayerNorm
    norm2: eqx.nn.LayerNorm

    def __init__(
        self, num_patches, hidden_size, mix_patch_size, mix_hidden_size,*,key):
        keys = jax.random.split(key, 2)
        self.patch_mixer = eqx.nn.MLP(
            num_patches, num_patches, mix_patch_size, depth=1, key=keys[0])
        self.hidden_mixer = eqx.nn.MLP(
            hidden_size, hidden_size, mix_hidden_size, depth=1, key=keys[1])
        self.norm1 = eqx.nn.LayerNorm((hidden_size, num_patches))
        self.norm2 = eqx.nn.LayerNorm((num_patches, hidden_size))
    def __call__(self, y):
        y = y + jax.vmap(self.patch_mixer)(self.norm1(y)) #residual
        y = einops.rearrange(y, "c p -> p c")
        y = y + jax.vmap(self.hidden_mixer)(self.norm2(y)) #residual
        y = einops.rearrange(y, "p c -> c p")
        return y

class Mixer2d(eqx.Module):
    conv_in: eqx.nn.Conv2d
    conv_out: eqx.nn.ConvTranspose2d
    blocks: list
    norm: eqx.nn.LayerNorm
    t1: float

    def __init__(self, img_size, patch_size, hidden_size, mix_patch_size, 
                 mix_hidden_size, num_blocks, t1, *, key,):
        input_size, height, width = img_size
        assert (height % patch_size) == 0
        assert (width % patch_size) == 0
        num_patches = (height // patch_size) * (width // patch_size)
        inkey, outkey, *bkeys = jax.random.split(key, 2 + num_blocks)

        self.conv_in = eqx.nn.Conv2d(
            input_size + 1, hidden_size, patch_size, stride=patch_size, key=inkey)
        self.conv_out = eqx.nn.ConvTranspose2d(
            hidden_size, input_size, patch_size, stride=patch_size, key=outkey)
        self.blocks = [
            MixerBlock(num_patches, hidden_size, mix_patch_size, 
                       mix_hidden_size, key=bkey)
            for bkey in bkeys]
        self.norm = eqx.nn.LayerNorm((hidden_size, num_patches))
        self.t1 = t1

    def __call__(self, t, y):
        t = t / self.t1
        _, height, width = y.shape
        t = einops.repeat(t, "-> 1 h w", h=height, w=width)
        y = jnp.concatenate([y, t])
        y = self.conv_in(y)
        _, patch_height, patch_width = y.shape
        y = einops.rearrange(y, "c h w -> c (h w)")
        for block in self.blocks:
            y = block(y)
        y = self.norm(y)
        y = einops.rearrange(y, "c (h w) -> c h w", h=patch_height, w=patch_width)
        return self.conv_out(y)
    
def single_loss_fn(model, weight, int_beta, data, t, key):
    mean = data * jnp.exp(-0.5 * int_beta(t))
    var = jnp.maximum(1 - jnp.exp(-int_beta(t)), 1e-5)
    std = jnp.sqrt(var)
    noise = jax.random.normal(key, data.shape)
    y = mean + std * noise
    pred = model(t, y)
    return weight(t) * jnp.mean((pred + noise / std) ** 2)


def batch_loss_fn(model, weight, int_beta, data, t1, key):
    batch_size = data.shape[0]
    tkey, losskey = jax.random.split(key)
    losskey = jax.random.split(losskey, batch_size)
    t = jax.random.uniform(tkey, (batch_size,), minval=0, maxval=t1 / batch_size)
    t = t + (t1 / batch_size) * jnp.arange(batch_size)
    loss_fn = partial(single_loss_fn, model, weight, int_beta)
    loss_fn = jax.vmap(loss_fn)
    return jnp.mean(loss_fn(data, t, losskey))


@eqx.filter_jit
def single_sample_fn(model, int_beta, data_shape, dt0, t1, key):
    def drift(t, y, args):
        _, beta = jax.jvp(int_beta, (t,), (jnp.ones_like(t),))
        return -0.5 * beta * (y + model(t, y))

    term = dx.ODETerm(drift)
    solver = dx.Tsit5()
    t0 = 0
    y1 = jax.random.normal(key, data_shape)
    # reverse time, solve from t1 to t0
    sol = dx.diffeqsolve(term, solver, t1, t0, -dt0, y1)
    return sol.ys[0]

@eqx.filter_jit
def make_step(model, weight, int_beta, data, t1, key, opt_state, optim):
    loss_fn = eqx.filter_value_and_grad(batch_loss_fn)
    loss, grads = loss_fn(model, weight, int_beta, data, t1, key)
    updates, opt_state = optim.update(grads, opt_state)
    model = eqx.apply_updates(model, updates)
    key = jax.random.split(key, 1)[0]
    return loss, model, key, opt_state


if __name__ == '__main__':
    
    ############ HYPERPARAMS #############

    # Model hyperparameters
    patch_size=4
    hidden_size=64
    mix_patch_size=512
    mix_hidden_size=512
    num_blocks=4
    t1=10.0
    # Optimisation hyperparameters
    num_steps=1000000
    lr=2e-4
    batch_size=256
    print_every=1000
    # Sampling hyperparameters
    dt0=0.1
    sample_size=10

    
    ############### DATA ################

    key = jax.random.PRNGKey(1736)
    model_key, train_key, loader_key, sample_key = jax.random.split(key, 4)
    data = load_mnist()
    data_mean = jnp.mean(data)
    data_std = jnp.std(data)
    data_max = jnp.max(data)
    data_min = jnp.min(data)
    data_shape = data.shape[1:]
    data = (data - data_mean) / data_std


    ############## MODEL #################

    model = Mixer2d(data_shape, patch_size, hidden_size, mix_patch_size, 
                    mix_hidden_size, num_blocks, t1, key=model_key)
    int_beta = lambda t: t 
    weight = lambda t: 1 - jnp.exp(-int_beta(t))

    ############### OPTIM ################

    optim = optax.adabelief(lr)
    opt_state = optim.init(eqx.filter(model, eqx.is_inexact_array))


    ############### TRAINING ###############

    total_value = 0
    total_size = 0
    losses = []
    for step, data in zip(
        range(num_steps), dataloader(data, batch_size, key=loader_key)):
        
        value, model, train_key, opt_state = make_step(
            model, weight, int_beta, data, t1, train_key, opt_state, optim)
        total_value += value.item()
        total_size += 1
        loss = total_value / total_size
        losses.append(loss)
        if (step % print_every) == 0 or step == num_steps - 1:
            print(f"Step={step}/{num_steps}, Loss={loss}")
            total_value = 0
            total_size = 0
    
    fig, ax = plt.subplots()
    ax.plot(losses, label = 'losses')
    ax.set(title = 'Diffusion losses vs iter', xlabel = 'iter', ylabel = 'loss')
    ax.legend()
    fig.savefig('./assets/diffusion_losses.png', dpi=300)


    ############ LOAD PRETRAINED ############

    # model = eqx.tree_deserialise_leaves('./models/diffusion.eqx', models)


    ############### INFERENCE ###############

    sample_key = jax.random.split(sample_key, sample_size**2)
    sample_fn = partial(single_sample_fn, model, int_beta, data_shape, dt0, t1)
    sample = jax.vmap(sample_fn)(sample_key)
    sample = data_mean + data_std * sample
    sample = jnp.clip(sample, data_min, data_max)
    sample = einops.rearrange(sample, "(n1 n2) 1 h w -> (n1 h) (n2 w)", 
                              n1=sample_size, n2=sample_size)
    
    fig, ax = plt.subplots()
    ax.imshow(sample, cmap="Greys")
    ax.set_title('Diffusion')
    ax.axis("off")
    fig.tight_layout()
    fig.savefig('./assets/diffusion_sample.png', dpi=300)
    plt.close()


    ################ SAVING ##################

    eqx.tree_serialise_leaves('./models/diffusion.eqx', model)